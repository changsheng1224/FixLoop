"""上下文预算管理：Token 级精确计数 + Prompt 组装 + 历史压缩。

按 model/provider 选择 tokenizer（DeepSeek HF / OpenAI tiktoken），中文计数更准确。

裁剪优先级：relevant_note → history → memory → prefix。
用户请求永不裁剪。
"""

from __future__ import annotations

from agent_runtime.compression_pipeline import (
    DEFAULT_TOOL_TRUNCATION,
    L5_TRIGGER_RATIO,
    apply_l1_to_request_text,
    l5_auto_compact,
    make_summarizer,
    run_compression_pipeline,
    truncate_tool_content as _truncate_tool_content,
)
from agent_runtime.tier_policy import TierPolicy, filter_relevant_results
from agent_runtime.tokenizers import resolve_token_counter

# Section token 预算分配（以 REF_TOTAL_BUDGET 为参考布局，随 prompt_budget 等比缩放）
REF_TOTAL_BUDGET = 6000
TOTAL_BUDGET = 100_000
BUDGET_PREFIX = 2000
BUDGET_MEMORY = 800
BUDGET_RELEVANT = 600
BUDGET_HISTORY = 2600
KEEP_RECENT_HISTORY = 6  # 最近 N 条历史完整保留


def scaled_section_budget(section_limit: int, total_limit: int) -> int:
    """将参考布局下的 section 预算缩放到实际 total_limit。"""
    return max(1, int(section_limit * total_limit / REF_TOTAL_BUDGET))


def history_window_budget(total_limit: int) -> int:
    """history section 预算 = 压缩管线 window（L2–L5 百分比阈值基准）。"""
    return scaled_section_budget(BUDGET_HISTORY, total_limit)


class TokenBudget:
    """Token 精确计数器（多 backend：DeepSeek HF / OpenAI tiktoken）。"""

    def __init__(
        self,
        model: str = "deepseek-v4-pro",
        total_limit: int = TOTAL_BUDGET,
        provider: str = "deepseek",
    ):
        self.total_limit = total_limit
        self.model = model
        self.provider = provider
        self._counter = resolve_token_counter(model, provider)
        self.backend = self._counter.backend

    def count(self, text: str) -> int:
        """返回文本的 token 数。"""
        return self._counter.count(text)

    def fit(self, text: str, limit: int) -> str:
        """将文本截断到指定 token 限制以内。"""
        return self._counter.fit(text, limit)

    def remaining(self, used: int) -> int:
        """返回剩余 token 预算。"""
        return max(0, self.total_limit - used)


def fit_prompt_to_budget(
    system_text: str,
    user_text: str,
    *,
    model: str = "deepseek-v4-pro",
    provider: str = "deepseek",
    total_limit: int = TOTAL_BUDGET,
) -> tuple[str, str, dict]:
    """将 system + user 对 fit 到统一 token 预算内。"""
    budget = TokenBudget(model=model, total_limit=total_limit, provider=provider)
    metadata: dict = {
        "sections": {},
        "cuts": [],
        "budget": total_limit,
        "tokenizer_backend": budget.backend,
    }

    system_text = system_text or ""
    user_text = user_text or ""

    sys_tokens = budget.count(system_text)
    if sys_tokens >= total_limit:
        sys_cap = max(256, total_limit // 2)
        system_text = budget.fit(system_text, sys_cap)
        sys_tokens = budget.count(system_text)
        metadata["cuts"].append(f"裁剪 system 到 {sys_tokens} tokens")
    metadata["sections"]["system"] = sys_tokens

    remaining = budget.remaining(sys_tokens)
    user_tokens = budget.count(user_text)
    if user_tokens > remaining:
        user_text = budget.fit(user_text, remaining)
        user_tokens = budget.count(user_text)
        metadata["cuts"].append(f"裁剪 user 到 {user_tokens} tokens（剩余预算 {remaining}）")
    metadata["sections"]["user"] = user_tokens
    metadata["total_tokens"] = sys_tokens + user_tokens
    return system_text, user_text, metadata


class ContextManager:
    """Prompt 组装器：按预算拼接 5 个 section，超限时自动裁剪。

    Sections:
    1. prefix       (~2000 tokens) — System Prompt + 工具列表 + Workspace
    2. memory       (~800 tokens)  — 工作记忆
    3. relevant     (~600 tokens)  — 相关记忆条目
    4. history      — 对话/工具调用历史（预算随 prompt_budget 缩放）
    5. request      (不限制)       — 当前用户输入
    """

    def __init__(self, agent, total_budget: int | None = None):
        self.agent = agent
        limit = total_budget
        if limit is None:
            limit = getattr(getattr(agent, "config", None), "prompt_budget", TOTAL_BUDGET)
        model = getattr(getattr(agent, "config", None), "model", "deepseek-v4-pro")
        provider = getattr(getattr(agent, "config", None), "provider", "deepseek")
        self.budget = TokenBudget(model=model, total_limit=limit, provider=provider)
        self._summary_cache: dict[str, str] = {}
        self.tier_policy = TierPolicy.from_agent(agent)

    def build(self, user_message: str) -> tuple[str, dict]:
        """组装完整 prompt，返回 (prompt_text, metadata)。

        metadata 含各 section token 数、裁剪日志和 prompt_cache_key。
        """
        metadata = {"sections": {}, "cuts": []}

        # Prompt Cache key = prefix hash
        metadata["prompt_cache_key"] = getattr(self.agent._prefix, "hash", "")

        # 收集各 section 源文本
        prefix_text = self._get_prefix()
        memory_text = self._get_memory()
        relevant_text = self._get_relevant(user_message)
        history_text = self._get_compressed_history(metadata)

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

        # 按优先级添加（section 预算随 total_limit 等比缩放）
        total = self.budget.total_limit
        add_section("prefix", prefix_text, scaled_section_budget(BUDGET_PREFIX, total))
        add_section("memory", memory_text, scaled_section_budget(BUDGET_MEMORY, total))
        add_section("relevant", relevant_text, scaled_section_budget(BUDGET_RELEVANT, total))
        add_section("history", history_text, history_window_budget(total))
        user_message = apply_l1_to_request_text(user_message, self.budget)
        request_tokens = self.budget.count(user_message)
        used += request_tokens
        metadata["sections"]["request"] = request_tokens
        if used > self.budget.total_limit:
            allowed_request = max(256, self.budget.total_limit - (used - request_tokens))
            if request_tokens > allowed_request:
                user_message = self.budget.fit(user_message, allowed_request)
                request_tokens = self.budget.count(user_message)
                used = used - metadata["sections"]["request"] + request_tokens
                metadata["sections"]["request"] = request_tokens
                metadata["cuts"].append(f"裁剪 request 到 {request_tokens} tokens")
        result_parts.append(f"\n## 当前任务\n\n{user_message}")

        metadata["total_tokens"] = used
        metadata["budget"] = self.budget.total_limit
        metadata["tokenizer_backend"] = self.budget.backend
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

        # Episodic 检索（L0：低分条目不注入）
        mem = self.agent.session.get("memory", {})
        results = retrieval_candidates_semantic(mem, query, limit=2)
        results = filter_relevant_results(results, self.tier_policy)
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

    def _get_compressed_history(self, metadata: dict | None = None) -> str:
        """获取压缩后的对话历史（L0–L5 管线，L5 在 L1–L4 之后）。"""
        history = self.agent.session.get("history", [])
        if not history:
            return ""

        meta = metadata if metadata is not None else {}

        projected = run_compression_pipeline(
            history,
            self.budget,
            metadata=meta,
            summarizer=make_summarizer(self.agent),
            summary_cache=self._summary_cache,
            history_window=history_window_budget(self.budget.total_limit),
            tier_policy=self.tier_policy,
        )

        pipe = meta.get("compression_pipeline", {})
        if pipe.get("l5_triggered"):
            return self._format_compressed_result(projected, apply_l1=False)

        recent = projected[-KEEP_RECENT_HISTORY:]
        old = projected[:-KEEP_RECENT_HISTORY]

        lines = ["## 对话历史", ""]

        if old and not any(
            pipe.get(k)
            for k in ("l2_triggered", "l3_triggered", "l4_triggered")
        ):
            compressed = self._compress_old_entries(old)
            if compressed:
                lines.append("### 早期摘要")
                lines.append(compressed)
                lines.append("")

        lines.append("### 最近对话")
        for item in recent:
            role = item.get("role", "unknown")
            content = str(item.get("content", ""))
            if role == "user" and len(content) > 300:
                content = content[:300] + "..."
            lines.append(f"**{role}**: {content}")
            lines.append("")

        return "\n".join(lines)

    def _format_compressed_result(self, history: list, *, apply_l1: bool = False) -> str:
        """将 history 列表格式化为 prompt 文本。"""
        lines = ["## 对话历史", ""]
        for item in history:
            role = item.get("role", "unknown")
            content = str(item.get("content", ""))
            if apply_l1 and role == "tool":
                tool_name = item.get("tool_name", "")
                content = _truncate_tool_content(content, tool_name, budget=self.budget)
            elif self.budget.count(content) > DEFAULT_TOOL_TRUNCATION:
                content = self.budget.fit(content, DEFAULT_TOOL_TRUNCATION) + "..."
            lines.append(f"**{role}**: {content}")
            lines.append("")
        return "\n".join(lines)

    def _maybe_summarize_history(self, history: list, trigger_tokens: int | None = None) -> list:
        """当 history token 数超阈值时，用 LLM 压缩前一半为摘要（L5 薄封装）。

        成功：返回 [{"role":"system","content":"[Earlier summary]: ..."}, *recent]
        失败：退化为简单裁剪（保留最近 8 条）
        """
        if trigger_tokens is None:
            trigger_tokens = int(
                L5_TRIGGER_RATIO * history_window_budget(self.budget.total_limit)
            )
        meta: dict = {}
        return l5_auto_compact(
            history,
            self.budget,
            meta,
            summarizer=make_summarizer(self.agent),
            summary_cache=self._summary_cache,
            trigger_tokens=trigger_tokens,
            history_window=history_window_budget(self.budget.total_limit),
        )

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
