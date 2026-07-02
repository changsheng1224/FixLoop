"""上下文预算管理：Token 级精确计数 + Prompt 组装 + 历史压缩。

使用 tiktoken 替代字符数估算，中文场景误差从 3-5 倍降低到 < 5%。

裁剪优先级：relevant_note → history → memory → prefix。
用户请求永不裁剪。
"""

import tiktoken

# 按工具类型的截断上限（字符数）
TOOL_TRUNCATION = {
    "list_files": 200,
    "search": 800,
    "read_file": 2000,
    "write_file": 300,
    "patch_file": 300,
    "run_shell": 500,
}
DEFAULT_TRUNCATION = 500


def _truncate_tool_content(content: str, tool_name: str = "") -> str:
    """按工具类型差异化截断，重要行优先保留。"""
    limit = TOOL_TRUNCATION.get(tool_name, DEFAULT_TRUNCATION)
    if len(content) <= limit:
        return content

    lines = content.splitlines()
    # 收集重要行（Error、文件路径、行号）
    important = []
    other = []
    for line in lines:
        if "Error" in line or "error" in line or "Fail" in line or "/" in line:
            important.append(line)
        else:
            other.append(line)

    result = important.copy()
    for line in other:
        result.append(line)
        if sum(len(ln) for ln in result) > limit:
            break

    return "\n".join(result) + f"\n... (截断，共 {len(content)} 字符)"


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
        self._summary_cache: dict[str, str] = {}

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
        relevant_text = self._get_relevant(user_message)
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
        """Working Memory：当前任务 + 最近文件 + 文件摘要。"""
        mem = self.agent.session.get("memory", {})
        working = mem.get("working", {})
        parts = []

        task = working.get("task_summary", "")
        if task:
            parts.append(f"任务: {task}")

        files = working.get("recent_files", [])
        if files:
            parts.append(f"最近文件: {', '.join(files[-5:])}")

        summaries = mem.get("file_summaries", {})
        if summaries:
            lines = []
            for path, info in list(summaries.items())[-3:]:
                if isinstance(info, dict):
                    lines.append(f"  {path}: {info.get('summary', '')[:100]}")
            if lines:
                parts.append("文件摘要:\n" + "\n".join(lines))

        return "\n".join(parts) if parts else ""

    def _get_relevant(self, query: str = "") -> str:
        """Episodic + Durable Memory 检索：与当前查询相关的笔记和持久知识。"""
        if not query:
            return ""
        from agent_runtime.features.memory import (
            DurableMemoryStore,
            retrieval_candidates_semantic,
        )
        parts = []

        # Episodic 检索
        mem = self.agent.session.get("memory", {})
        results = retrieval_candidates_semantic(mem, query, limit=2)
        if results:
            lines = ["相关记忆:"]
            for r in results:
                lines.append(f"  - {r.get('text', '')[:150]}")
            parts.append("\n".join(lines))

        # Durable 检索
        try:
            store = DurableMemoryStore(root=self.agent._cwd)
            durable_results = store.retrieval(query, limit=2)
            if durable_results:
                lines = ["持久知识:"]
                for r in durable_results:
                    lines.append(f"  - {r[:150]}")
                parts.append("\n".join(lines))
        except Exception:
            pass

        return "\n".join(parts) if parts else ""

    def _get_compressed_history(self) -> str:
        """获取压缩后的对话历史。优先 LLM 摘要，降级为规则压缩。"""
        history = self.agent.session.get("history", [])
        if not history:
            return ""

        # 先尝试 LLM 摘要（超 2600 tokens 时触发）
        summarized = self._maybe_summarize_history(history)
        if summarized is not history:  # 返回了新列表（触发了摘要）
            return self._format_compressed_result(summarized)

        # 降级：规则压缩
        recent = history[-KEEP_RECENT_HISTORY:]
        old = history[:-KEEP_RECENT_HISTORY]

        lines = ["## 对话历史", ""]

        if old:
            compressed = self._compress_old_entries(old)
            if compressed:
                lines.append("### 早期摘要")
                lines.append(compressed)
                lines.append("")

        lines.append("### 最近对话")
        for item in recent:
            role = item.get("role", "unknown")
            content = str(item.get("content", ""))
            if role == "tool":
                tool_name = item.get("tool_name", "")
                content = _truncate_tool_content(content, tool_name)
            if role == "user" and len(content) > 300:
                content = content[:300] + "..."
            lines.append(f"**{role}**: {content}")
            lines.append("")

        return "\n".join(lines)

    def _format_compressed_result(self, history: list) -> str:
        """将 _maybe_summarize_history 的输出格式化为 prompt 文本。"""
        lines = ["## 对话历史", ""]
        for item in history:
            role = item.get("role", "unknown")
            content = str(item.get("content", ""))
            if role == "tool":
                tool_name = item.get("tool_name", "")
                content = _truncate_tool_content(content, tool_name)
            elif len(content) > DEFAULT_TRUNCATION:
                content = content[:DEFAULT_TRUNCATION] + "..."
            lines.append(f"**{role}**: {content}")
            lines.append("")
        return "\n".join(lines)

    def _maybe_summarize_history(self, history: list, trigger_tokens: int = 2600) -> list:
        """当 history token 数超阈值时，用 LLM 压缩前一半为摘要。

        成功：返回 [{"role":"system","content":"[Earlier summary]: ..."}, *recent]
        失败：退化为简单裁剪（保留最近 8 条）

        Args:
            history: 原始 history 列表。
            trigger_tokens: 触发摘要的 token 阈值。

        Returns:
            压缩后的 history 列表。
        """
        # 序列化并计数
        raw_text = self._format_history_text(history)
        token_count = self.budget.count(raw_text)

        if token_count <= trigger_tokens:
            return history

        # 取前一半作为"旧历史"
        mid = len(history) // 2
        old_history = history[:mid]
        recent_history = history[mid:]

        # 检查摘要缓存
        import hashlib
        cache_key = hashlib.md5(
            "".join(str(h.get("content",""))[:100] for h in old_history[-10:]).encode()
        ).hexdigest()
        if cache_key in self._summary_cache:
            summary = self._summary_cache[cache_key]
        else:
            summary = ""
            try:
                summary = self._generate_summary(old_history)
            except Exception:
                pass
            if summary:
                self._summary_cache[cache_key] = summary

        if summary:
            return [
                {"role": "system", "content": f"[Earlier summary]: {summary}"},
            ] + recent_history

        # 降级：保留最近 8 条
        return history[-8:]

    def _format_history_text(self, history: list) -> str:
        """将 history 列表格式化为纯文本（用于计数）。"""
        lines = []
        for item in history:
            role = item.get("role", "unknown")
            content = str(item.get("content", ""))[:200]
            lines.append(f"{role}: {content}")
        return "\n".join(lines)

    def _generate_summary(self, old_history: list) -> str:
        """用模型生成旧历史的摘要（最多 200 tokens）。"""
        prompt_lines = [
            "Summarize the following conversation in 1-2 sentences.",
            "Focus on: files read, tools used, errors encountered, decisions made.",
            "",
        ]
        # 只取关键信息
        for item in old_history[-20:]:
            role = item.get("role", "unknown")
            content = str(item.get("content", ""))[:150]
            prompt_lines.append(f"{role}: {content}")

        summary_prompt = "\n".join(prompt_lines)

        # 优先用轻量模型（本地 Ollama），更快且不消耗 API 配额
        client = self.agent.light_client or self.agent.model_client
        # 高配额兼容 thinking 模型（thinking ~2000 tokens）
        raw = client.complete(summary_prompt, max_new_tokens=2048)
        return raw.strip()[:300] if raw else ""

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
