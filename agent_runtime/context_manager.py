"""上下文预算管理：Token 级精确计数 + Prompt 组装 + 历史压缩。

按 model/provider 选择 tokenizer（DeepSeek HF / OpenAI tiktoken），中文计数更准确。

裁剪优先级（后填先裁）：history → relevant → memory → workspace；system 与 request 优先保留。
"""

from __future__ import annotations

from pathlib import Path

from agent_runtime.compression_pipeline import (
    DEFAULT_TOOL_TRUNCATION,
    L5_TRIGGER_RATIO,
    apply_l1_to_request_text,
    l5_auto_compact,
    make_summarizer,
    run_compression_pipeline,
)
from agent_runtime.compression_pipeline import (
    truncate_tool_content as _truncate_tool_content,
)

# Re-export fit helpers for test / L2 import compatibility.
from agent_runtime.context_fit import fit_prompt_to_budget, fit_repair_user_prompt  # noqa: F401
from agent_runtime.context_projection import attach_context_projection
from agent_runtime.context_runtime import ContextItem, ContextPolicyEngine, ContextRequest
from agent_runtime.errors import ContextTooLargeError
from agent_runtime.message_projection import (
    get_sealed_history,
    run_memory_snapshot,
    run_user_query,
    seal_history_at_build,
)
from agent_runtime.section_filler import SectionFiller
from agent_runtime.task_section import (
    render_task_message,
    reserve_section_budget,
    task_preservation_metadata,
)
from agent_runtime.tier_policy import TierPolicy, filter_relevant_results
from agent_runtime.tokenizers import resolve_token_counter, resolve_tokenizer_spec

# Section token 预算分配（以 REF_TOTAL_BUDGET 为参考布局，随 prompt_budget 等比缩放）
REF_TOTAL_BUDGET = 6000
TOTAL_BUDGET = 100_000
BUDGET_PREFIX = 2000
BUDGET_SYSTEM = 700
BUDGET_TOOLS = 900
BUDGET_SKILLS = 400
BUDGET_MEMORY = 800
BUDGET_KNOWLEDGE = 600  # 持久知识检索（episodic notes + durable facts）
BUDGET_HISTORY = 2600
KEEP_RECENT_HISTORY = 6  # 最近 N 条历史完整保留
HARD_CAP = 8000  # 硬顶 token 数，超出拒绝 ask


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
        self._spec = resolve_tokenizer_spec(model, provider)
        self.tokenizer_fallback = self._spec.fallback
        self.tokenizer_id = self._spec.tokenizer_id

    def count(self, text: str) -> int:
        """返回文本的 token 数。"""
        return self._counter.count(text)

    def fit(self, text: str, limit: int) -> str:
        """将文本截断到指定 token 限制以内。"""
        return self._counter.fit(text, limit)

    def remaining(self, used: int) -> int:
        """返回剩余 token 预算。"""
        return max(0, self.total_limit - used)


class _DiskCache(dict):
    """dict-like 磁盘缓存（key → .agent/summary_cache/<hash>.txt）。

    内部用 content_hash 作为存储 key，外部透明使用原始 key。
    写失败静默降级内存 dict。
    """

    def __init__(self, cache_dir: Path):
        super().__init__()
        self._dir = cache_dir
        self._dir.mkdir(parents=True, exist_ok=True)
        self._load()

    @staticmethod
    def _hash_key(key: str) -> str:
        import hashlib

        return hashlib.sha256(key.encode()).hexdigest()[:16]

    def _path(self, key: str) -> Path:
        return self._dir / f"{self._hash_key(key)}.txt"

    def _load(self):
        for p in self._dir.glob("*.txt"):
            try:
                content = p.read_text(encoding="utf-8")
                lines = content.split("\n", 1)
                if len(lines) == 2:
                    super().__setitem__(lines[0], lines[1])
                elif len(lines) == 1:
                    # 旧格式兼容（单行 value，key=filename_stem）
                    super().__setitem__(p.stem, lines[0])
            except Exception:
                pass

    def __setitem__(self, key, value):
        super().__setitem__(key, value)
        try:
            # 文件格式：第一行原始 key，其余 value
            self._path(key).write_text(f"{key}\n{value}", encoding="utf-8")
        except Exception:
            pass


class ContextManager:
    """Prompt 组装器：按预算拼接 section，超限时自动裁剪。

    Sections（填充顺序）:
    1. system     — persona / rules（stable，可缓存）
    2. tools      — 工具签名（stable，可缓存）
    3. skills     — 调用示例 + L2 role
    4. workspace  — Workspace 快照（可变）
    5. memory     (~800 tokens)  — 工作记忆
    6. knowledge (~600 tokens)  — 持久知识检索（episodic + durable）
    7. history   — 对话/工具调用历史（预算随 prompt_budget 缩放）
    8. request   — 当前用户输入（L1 截断后）
    """

    SECTION_ORDER = (
        "system",
        "tools",
        "skills",
        "workspace",
        "memory",
        "knowledge",  # 持久知识检索（episodic notes + durable facts）
        "history",
        "state",
    )
    NATIVE_SYSTEM_ORDER = ("system", "tools")
    DYNAMIC_ORDER = ("skills", "workspace", "memory", "knowledge", "history", "state")

    def __init__(
        self,
        agent,
        total_budget: int | None = None,
        *,
        budget: TokenBudget | None = None,
    ):
        self.agent = agent
        if budget is not None:
            self.budget = budget
        elif getattr(agent, "_budget", None) is not None:
            self.budget = agent._budget
        else:
            limit = total_budget
            if limit is None:
                limit = getattr(getattr(agent, "config", None), "prompt_budget", TOTAL_BUDGET)
            model = getattr(getattr(agent, "config", None), "model", "deepseek-v4-pro")
            provider = getattr(getattr(agent, "config", None), "provider", "deepseek")
            self.budget = TokenBudget(model=model, total_limit=limit, provider=provider)
        self.hard_cap = int(
            getattr(getattr(agent, "config", None), "hard_cap", HARD_CAP) or HARD_CAP
        )
        cache_dir = Path(getattr(agent, "_cwd", ".")) / ".agent" / "summary_cache"
        self._summary_cache: dict[str, str] = _DiskCache(cache_dir)
        self.tier_policy = TierPolicy.from_agent(agent)

    def _check_hard_cap(self, used: int) -> None:
        """检查总 token 数是否超出硬顶；超出则抛 ContextTooLargeError。"""
        if used > self.hard_cap:
            raise ContextTooLargeError(actual=used, limit=self.hard_cap)

    def build(self, user_message: str) -> tuple[str, dict]:
        """组装完整 prompt，返回 (prompt_text, metadata)。

        metadata 含各 section token 数、裁剪日志和 prompt_cache_key。

        Raises:
            ContextTooLargeError: 若合计 tokens 超出硬顶限制。
        """
        metadata = self._base_metadata()
        sections = self._fill_sections(user_message, metadata)
        self._check_hard_cap(metadata.get("total_tokens", 0))
        result_parts = [sections[name] for name in self.SECTION_ORDER if sections.get(name)]
        if sections.get("request"):
            result_parts.append(sections["request"])
        return "\n".join(result_parts), metadata

    def build_dynamic_context(self, user_message: str) -> tuple[str, dict]:
        """组装动态上下文（不含 system / request）。

        Raises:
            ContextTooLargeError: 若合计 tokens 超出硬顶限制。
        """
        metadata = self._base_metadata()
        sections = self._fill_sections(
            user_message, metadata, include_system=False, include_request=False
        )
        self._check_hard_cap(metadata.get("total_tokens", 0))
        parts = [sections[name] for name in self.DYNAMIC_ORDER if sections.get(name)]
        return "\n\n".join(parts), metadata

    def build_for_native(self, user_message: str) -> tuple[str, str, dict]:
        """Native API：stable system+tools + 动态 user 上下文（含 skills/task）。

        system/skills 使用 native 规则与示例（禁止 XML 工具协议），与 text 路径前缀分离。

        Raises:
            ContextTooLargeError: 若合计 tokens 超出硬顶限制。
        """
        metadata = self._base_metadata()
        sections = self._fill_sections(user_message, metadata, native_tools=True)
        self._check_hard_cap(metadata.get("total_tokens", 0))
        system_parts = [sections[name] for name in self.NATIVE_SYSTEM_ORDER if sections.get(name)]
        system_prompt = "\n\n".join(system_parts)
        user_parts = [sections[name] for name in self.DYNAMIC_ORDER if sections.get(name)]
        if sections.get("request"):
            user_parts.append(sections["request"])
        return system_prompt, "\n\n".join(user_parts), metadata

    def _base_metadata(self) -> dict:
        from agent_runtime.prompt_prefix import build_prefix_hashes

        prefix = self.agent._prefix
        prefix_hashes = build_prefix_hashes(prefix)
        return {
            "sections": {},
            "cuts": [],
            "prompt_cache_key": prefix_hashes["cache_key"],
            "prefix_hashes": prefix_hashes,
        }

    def _fill_sections(
        self,
        user_message: str,
        metadata: dict,
        *,
        include_system: bool = True,
        include_request: bool = True,
        native_tools: bool = False,
    ) -> dict[str, str]:
        """按预算填充各 section，返回 name → 文本。"""
        total = self.budget.total_limit
        request_text = ""
        request_tokens = 0
        section_cap = total

        if include_request:
            processed = apply_l1_to_request_text(user_message, self.budget)
            request_text, tpl_meta = render_task_message(
                processed,
                repo_root=self._agent_repo_root(),
            )
            metadata.update(tpl_meta)
            request_tokens = self.budget.count(request_text)
            metadata["sections"]["request"] = request_tokens
            metadata.update(task_preservation_metadata(request_tokens, total))
            section_cap = reserve_section_budget(total, request_tokens)

        filler = SectionFiller(
            self.budget,
            metadata,
            section_cap=section_cap,
            total_limit=total,
            scaled_budget=scaled_section_budget,
        )

        if include_system:
            if native_tools:
                filler.add_stable_section(
                    "system", self._get_system_for_native(), BUDGET_SYSTEM
                )
                filler.add_stable_section("tools", self._get_tools(), BUDGET_TOOLS)
                filler.add_stable_section(
                    "skills", self._get_skills_for_native(), BUDGET_SKILLS
                )
            else:
                filler.add_stable_section("system", self._get_system(), BUDGET_SYSTEM)
                filler.add_stable_section("tools", self._get_tools(), BUDGET_TOOLS)
                filler.add_stable_section("skills", self._get_skills(), BUDGET_SKILLS)
        filler.add_section(
            "workspace",
            self._get_workspace(),
            scaled_section_budget(BUDGET_PREFIX, section_cap or total),
        )
        filler.add_section(
            "state",
            self._get_state(),
            200,  # state 段固定 200 token 预算
        )
        filler.add_section(
            "memory",
            self._get_memory(),
            scaled_section_budget(BUDGET_MEMORY, section_cap or total),
        )
        filler.add_section(
            "knowledge",
            self._get_knowledge(user_message),
            scaled_section_budget(BUDGET_KNOWLEDGE, section_cap or total),
        )
        history_text = self._get_compressed_history(metadata)
        metadata["_history_section_text"] = history_text
        filler.add_section(
            "history",
            history_text,
            history_window_budget(section_cap or total),
        )

        sections = dict(filler.sections)
        used = filler.used

        if include_request:
            sections["request"] = request_text
            used += request_tokens

        metadata["_context_prefix_text"] = "\n".join(
            sections[name] for name in self.SECTION_ORDER if sections.get(name)
        )

        sys_tokens = metadata["sections"].get("system", 0)
        tools_tokens = metadata["sections"].get("tools", 0)
        skills_tokens = metadata["sections"].get("skills", 0)
        ws_tokens = metadata["sections"].get("workspace", 0)
        if sys_tokens or tools_tokens or skills_tokens or ws_tokens:
            metadata["sections"]["prefix"] = sys_tokens + tools_tokens + skills_tokens + ws_tokens

        metadata["total_tokens"] = used
        metadata["budget"] = self.budget.total_limit
        metadata["tokenizer_backend"] = self.budget.backend
        attach_context_projection(metadata, agent=self.agent, budget=self.budget)
        history = self.agent.read_history()  # JSONL 优先，build 不写回
        if history and history_text:
            seal_history_at_build(self.agent.session, len(history), history_text)
        return sections

    def _agent_repo_root(self) -> str | None:
        agent = self.agent
        cwd = getattr(agent, "_cwd", "") or ""
        if cwd:
            return cwd
        workspace = getattr(agent, "workspace", None)
        if workspace is None:
            return None
        return getattr(workspace, "repo_root", "") or getattr(workspace, "cwd", "") or None

    # ---- Section 收集 ----

    def _get_system(self) -> str:
        prefix = getattr(self.agent, "_prefix", None)
        if prefix is not None:
            return getattr(prefix, "stable_system_text", "") or ""
        return getattr(self.agent, "_system_prompt", "") or ""

    def _get_system_for_native(self) -> str:
        """Native：保留 persona / dry-run 等非协议段，规则换成 tool_use-only。"""
        from agent_runtime.prompt_external import default_rules_text

        original = self._get_system() or ""
        head_lines: list[str] = []
        for line in original.splitlines():
            if line.startswith("## "):
                break
            head_lines.append(line)
        head = "\n".join(head_lines).strip()
        # 仅保留 compose_rules 挂的运行时后缀（8./9.），勿把 persona 再抄一遍
        extras: list[str] = []
        for line in original.splitlines():
            s = line.strip()
            if s.startswith("8.") or s.startswith("9."):
                if "<function_calls>" in s or "<invoke" in s or "<tool>" in s:
                    continue
                extras.append(s)
        parts = [p for p in [head, default_rules_text(native_tools=True)] if p]
        if extras:
            parts.append("\n".join(extras))
        return "\n\n".join(parts)

    def _get_tools(self) -> str:
        prefix = getattr(self.agent, "_prefix", None)
        if prefix is None:
            return ""
        return getattr(prefix, "stable_tools_text", "") or ""

    def _get_skills(self) -> str:
        prefix = getattr(self.agent, "_prefix", None)
        if prefix is None:
            return ""
        parts = []
        skills = getattr(prefix, "stable_skills_text", "") or ""
        if skills:
            parts.append(skills)
        role = getattr(prefix, "role_text", "") or ""
        if role:
            parts.append(role)
        return "\n\n".join(parts)

    def _get_skills_for_native(self) -> str:
        """Native：示例改为 API tool_use；保留 L2 role。"""
        from agent_runtime.prompt_external import default_examples_text

        prefix = getattr(self.agent, "_prefix", None)
        role = ""
        if prefix is not None:
            role = getattr(prefix, "role_text", "") or ""
        parts = [default_examples_text(native_tools=True)]
        if role and "<function_calls>" not in role and "<invoke" not in role:
            parts.append(role)
        return "\n\n".join(parts)

    def _get_state(self) -> str:
        """返回当前 task state 摘要：task_summary + phase + plan_todos 前 3 条。

        经 section_filler 遵守 BUDGET_STATE (200 tokens)。超长时由 filler 截断。
        与 count_state_section() 共用 context_projection.format_state_text()。
        """
        from agent_runtime.context_projection import format_state_text

        return format_state_text(self.agent.session, agent=self.agent)

    def _get_workspace(self) -> str:
        workspace_text = getattr(self.agent._prefix, "workspace_text", "")
        if workspace_text:
            return workspace_text
        workspace = getattr(self.agent, "workspace", None)
        return workspace.text() if workspace else ""

    def _get_memory(self) -> str:
        """Memory 段：当前任务的临时工作记忆（task_summary + recent_files + file_summaries）。

        与 knowledge 段的区别：
        - memory：本次任务的临时上下文，每轮 ask() 重置。
        - knowledge：跨会话持久知识（episodic notes + durable facts），持久化存储。
        """
        from agent_runtime.features.memory import render_evidence_ledger, render_repair_context

        snap = run_memory_snapshot(self.agent.session)
        mem = snap if snap is not None else self.agent.session.get("memory", {})
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

        evidence = render_evidence_ledger(mem)
        if evidence:
            parts.append(evidence)

        repair_state = render_repair_context(mem)
        if repair_state:
            parts.insert(0, repair_state)

        return "\n".join(parts) if parts else ""

    def _get_knowledge(self, query: str = "") -> str:
        """Knowledge 段：三层 RAG 检索结果。

        Layer 1 (投影层): episodic notes → semantic/keyword → knowledge section
        Layer 2 (流水线): RepairPrecedentStore → similar_fixes (by Orchestrator)
        Layer 3 (缓存层): .agent/embed_cache → content_hash → embedding reuse

        三层共用 derive_embed_query() 作为查询提取入口。
        """
        query = run_user_query(self.agent.session, query)
        if not query:
            return ""
        from agent_runtime.features.memory import (
            DurableMemoryStore,
            retrieval_candidates_semantic,
        )

        parts = []
        request = ContextRequest(
            phase=str(getattr(self.agent, "_l2_phase", "repair") or "repair"),
            intent=query,
            token_budget=BUDGET_KNOWLEDGE,
        )
        policy = ContextPolicyEngine()

        # Layer 1: Episodic 检索
        mem = self.agent.session.get("memory", {})
        identity = mem.get("memory_identity") or {}
        from agent_runtime.features.memory.governance import MemoryGovernanceService

        governed = MemoryGovernanceService(
            mem,
            repo_root=str(getattr(self.agent, "_cwd", "") or ""),
            user_id=str(identity.get("user_id", "") or ""),
            task_id=str(identity.get("task_id", "") or ""),
        )
        governed_results = governed.recall(
            query,
            user_id=str(identity.get("user_id", "") or ""),
            task_id=str(identity.get("task_id", "") or ""),
            limit=2,
        )
        recalled_ids = [str(item.get("memory_id", "")) for item in governed_results]
        if governed_results:
            governed_items = [
                ContextItem(
                    item_id=item["memory_id"],
                    kind="memory",
                    content=str(item.get("value", "")),
                    source_ref=item["memory_id"],
                    token_cost=max(1, len(str(item.get("value", "")).split())),
                    relevance=float(item.get("score", 0.0)),
                    confidence=float(item.get("confidence", 0.0)),
                    freshness=1.0,
                    evidence_strength=1.0 if item.get("evidence_refs") else 0.0,
                    scope=str(item.get("scope", "task")),
                )
                for item in governed_results
            ]
            governed_items = policy.select(governed_items, request)
            mem["recalled_memory_ids"] = [
                item.source_ref for item in governed_items
            ]
            lines = ["治理记忆候选（必须由当前代码证据确认）:"]
            for item in governed_items:
                lines.append(
                    f"  - {item.content[:150]} "
                    f"[utility={item.utility(request):.4f} confidence={item.confidence:.2f}]"
                )
            parts.append("\n".join(lines))
        results = retrieval_candidates_semantic(mem, query, limit=2)
        results = filter_relevant_results(results, self.tier_policy)
        recalled_ids.extend(str(item.get("memory_id", "")) for item in results)
        if results:
            lines = ["历史经验候选（必须由当前代码证据确认，不是当前事实）:"]
            for r in results:
                lines.append(
                    f"  - {r.get('text', '')[:150]} "
                    f"[scope={r.get('scope', 'unknown')} "
                    f"confidence={float(r.get('confidence', 0.0)):.2f}]"
                )
            parts.append("\n".join(lines))

        # Layer 2: Durable 检索
        try:
            store = DurableMemoryStore(root=self.agent._cwd)
            durable_results = store.retrieval(query, limit=2)
            if durable_results:
                recalled_ids.extend(
                    str(item.get("memory_id", "")) for item in durable_results
                )
                lines = ["持久知识候选（仅项目/用户事实；冲突时不得采用）:"]
                for r in durable_results:
                    lines.append(f"  - {str(r.get('text', ''))[:150]}")
                parts.append("\n".join(lines))
        except Exception:
            pass

        # Keep one stable recall projection for feedback attribution.  Durable
        # markdown entries have no governed ID yet, so only governed IDs are
        # eligible for automatic usage feedback.
        recalled_ids = list(dict.fromkeys(item for item in recalled_ids if item))
        mem["recalled_memory_ids"] = recalled_ids
        governed.record_recall(recalled_ids, task_id=str(identity.get("task_id", "") or ""))
        return "\n".join(parts) if parts else ""

    def _get_compressed_history(self, metadata: dict | None = None) -> str:
        """获取压缩后的对话历史（L0–L5 管线；已封印段单调追加）。

        优先从 .agent/history.jsonl 读取，文件缺失时回退 session 内存。
        ContextManager 不写入 JSONL（写路径由 AgentLoop.record 独占）。
        """
        history = self.agent.read_history()
        if not history:
            return ""

        sealed_count, sealed_text = get_sealed_history(self.agent.session)
        if sealed_count > 0 and sealed_text:
            if sealed_count >= len(history):
                return sealed_text
            tail = history[sealed_count:]
            tail_text = self._format_projected_history(tail, metadata)
            if not tail_text:
                return sealed_text
            return f"{sealed_text.rstrip()}\n{tail_text}"

        return self._format_projected_history(history, metadata)

    def _format_projected_history(self, history: list, metadata: dict | None = None) -> str:
        """对 history 切片跑 L0–L5 并格式化为 history section 文本。"""
        if not history:
            return ""

        meta = metadata if metadata is not None else {}
        meta["_memory_state"] = self.agent.session.get("memory", {})
        sealed_count, sealed_text = get_sealed_history(self.agent.session)
        include_header = not (sealed_count > 0 and sealed_text)

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
            body = self._format_compressed_result(projected, apply_l1=False)
            return body if include_header else self._strip_history_header(body)

        return self._format_split_history(projected, pipe, include_header)

    def _format_split_history(self, projected: list, pipe: dict, include_header: bool) -> str:
        """格式化未触发 L5 的 history：早期摘要 + 最近对话。"""
        recent = projected[-KEEP_RECENT_HISTORY:]
        old = projected[:-KEEP_RECENT_HISTORY]
        lines: list[str] = []
        if include_header:
            lines.extend(["## 对话历史", ""])

        if old and not any(pipe.get(k) for k in ("l2_triggered", "l3_triggered", "l4_triggered")):
            compressed = self._compress_old_entries(old)
            if compressed:
                prefix = "### 早期摘要" if include_header else "### 追加早期摘要"
                lines.append(prefix)
                lines.append(compressed)
                lines.append("")

        if include_header:
            lines.append("### 最近对话")
        for item in recent:
            role = item.get("role", "unknown")
            content = str(item.get("content", ""))
            if role == "user" and len(content) > 300:
                content = content[:300] + "..."
            lines.append(f"**{role}**: {content}")
            lines.append("")
        return "\n".join(lines)

    @staticmethod
    def _strip_history_header(text: str) -> str:
        """去掉 history 段首行标题，便于单调追加。"""
        lines = text.splitlines()
        while lines and lines[0].strip() in ("## 对话历史", ""):
            lines.pop(0)
        while lines and lines[0].strip() == "### 最近对话":
            lines.pop(0)
        return "\n".join(lines).strip()

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
            trigger_tokens = int(L5_TRIGGER_RATIO * history_window_budget(self.budget.total_limit))
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
