"""上下文预算管理：Token 级精确计数 + Prompt 组装 + 历史压缩。

使用 tiktoken 替代字符数估算，中文场景误差从 3-5 倍降低到 < 5%。

裁剪优先级：relevant_note → history → memory → prefix。
用户请求永不裁剪。
"""

import tiktoken

# Section token 预算分配
BUDGET_PREFIX = 2000
BUDGET_MEMORY = 800
BUDGET_RELEVANT = 600
BUDGET_HISTORY = 2600
TOTAL_BUDGET = 6000
KEEP_RECENT_HISTORY = 6  # 最近 N 条历史完整保留


class TokenBudget:
    """Token 精确计数器。

    封装 tiktoken 编码器，提供 count / fit / remaining。
    """

    def __init__(self, model: str = "gpt-4", total_limit: int = TOTAL_BUDGET):
        self.total_limit = total_limit
        try:
            self.encoder = tiktoken.encoding_for_model(model)
        except KeyError:
            self.encoder = tiktoken.get_encoding("cl100k_base")

    def count(self, text: str) -> int:
        """返回文本的精确 token 数。"""
        return len(self.encoder.encode(text))

    def fit(self, text: str, limit: int) -> str:
        """将文本截断到指定 token 限制以内。

        Args:
            text: 原始文本。
            limit: token 上限。

        Returns:
            截断后的文本（token 数 <= limit）。
        """
        tokens = self.encoder.encode(text)
        if len(tokens) <= limit:
            return text
        return self.encoder.decode(tokens[:limit])

    def remaining(self, used: int) -> int:
        """返回剩余 token 预算。"""
        return max(0, self.total_limit - used)


class ContextManager:
    """Prompt 组装器：按预算拼接 5 个 section，超限时自动裁剪。

    Sections:
    1. prefix       (~2000 tokens) — System Prompt + 工具列表 + Workspace
    2. memory       (~800 tokens)  — 工作记忆
    3. relevant     (~600 tokens)  — 相关记忆条目
    4. history      (~2600 tokens) — 对话/工具调用历史
    5. request      (不限制)       — 当前用户输入
    """

    def __init__(self, agent, total_budget: int = TOTAL_BUDGET):
        self.agent = agent
        self.budget = TokenBudget(total_limit=total_budget)

    def build(self, user_message: str) -> tuple[str, dict]:
        """组装完整 prompt，返回 (prompt_text, metadata)。

        metadata 含各 section token 数、裁剪日志和 prompt_cache_key。
        """
        metadata = {"sections": {}, "cuts": []}

        # Prompt Cache key = prefix hash
        metadata["prompt_cache_key"] = getattr(
            self.agent._prefix, "hash", ""
        )

        # 收集各 section 源文本
        prefix_text = self._get_prefix()
        memory_text = self._get_memory()
        relevant_text = self._get_relevant()
        history_text = self._get_compressed_history()

        # 逐 section 填充，超限时按优先级裁剪
        used = 0
        result_parts = []

        def add_section(name: str, text: str, budget_limit: int):
            nonlocal used
            if used + self.budget.count(text) > self.budget.total_limit:
                # 需要裁剪
                remaining = self.budget.total_limit - used
                if remaining <= 0:
                    metadata["cuts"].append(f"跳过 {name}（预算耗尽）")
                    return
                text = self.budget.fit(text, remaining)
                metadata["cuts"].append(f"裁剪 {name} 到 {remaining} tokens")

            tokens = self.budget.count(text)
            used += tokens
            metadata["sections"][name] = tokens
            if text:
                result_parts.append(text)

        # 按优先级添加
        add_section("prefix", prefix_text, BUDGET_PREFIX)
        add_section("memory", memory_text, BUDGET_MEMORY)
        add_section("relevant", relevant_text, BUDGET_RELEVANT)
        add_section("history", history_text, BUDGET_HISTORY)
        # request 永不裁剪
        request_tokens = self.budget.count(user_message)
        used += request_tokens
        metadata["sections"]["request"] = request_tokens
        result_parts.append(f"\n## 当前任务\n\n{user_message}")

        metadata["total_tokens"] = used
        metadata["budget"] = self.budget.total_limit
        return "\n".join(result_parts), metadata

    # ---- Section 收集 ----

    def _get_prefix(self) -> str:
        return self.agent._prefix.text

    def _get_memory(self) -> str:
        # M3 工作记忆接入点
        return ""

    def _get_relevant(self) -> str:
        # M3 记忆检索接入点
        return ""

    def _get_compressed_history(self) -> str:
        """获取压缩后的对话历史。"""
        history = self.agent.session.get("history", [])
        if not history:
            return ""

        # 最近 KEEP_RECENT_HISTORY 条完整保留
        recent = history[-KEEP_RECENT_HISTORY:]
        old = history[:-KEEP_RECENT_HISTORY]

        lines = ["## 对话历史", ""]

        # 旧历史压缩
        if old:
            compressed = self._compress_old_entries(old)
            if compressed:
                lines.append("### 早期摘要")
                lines.append(compressed)
                lines.append("")

        # 近期历史完整保留
        lines.append("### 最近对话")
        for item in recent:
            role = item.get("role", "unknown")
            content = str(item.get("content", ""))
            # 截断过长的单条
            if role == "tool" and len(content) > 500:
                content = content[:500] + f"\n... (截断，共 {len(content)} 字符)"
            if role == "user" and len(content) > 300:
                content = content[:300] + "..."
            lines.append(f"**{role}**: {content}")
            lines.append("")

        return "\n".join(lines)

    def _compress_old_entries(self, entries: list) -> str:
        """压缩旧历史条目。

        - 重复 read_file 合并为一行
        - 旧工具结果压缩为单行摘要
        - 旧消息截断到 60 字符
        """
        items = []
        seen_reads = []
        for entry in entries:
            content = str(entry.get("content", ""))
            role = entry.get("role", "")

            if role == "assistant" and "read_file" in content:
                seen_reads.append(content.split("read_file")[-1].strip().rstrip(")"))
                continue

            if role == "tool":
                # 压缩为一行摘要
                first_line = content.split("\n")[0][:100]
                items.append(f"工具结果: {first_line}...")
                continue

            if role == "user":
                items.append(f"用户: {content[:60]}")
                continue

            items.append(f"{role}: {content[:60]}")

        result = []
        if seen_reads:
            result.append(f"已读取文件: {', '.join(seen_reads[:5])}")
        result.extend(items[-20:])  # 最多保留 20 条压缩项

        return "\n".join(result)
