"""上下文预算管理：Token 级精确计数 + Prompt 组装 + 历史压缩。

按 model/provider 选择 tokenizer（DeepSeek HF / OpenAI tiktoken），中文计数更准确。

Plan 请求保护完整必需内容，可选段使用软配额与回收池；普通 L1 沿用固定段预算。
"""

from __future__ import annotations

import copy
import hashlib
import json
import re
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
from agent_runtime.context_projection import attach_context_projection
from agent_runtime.context_runtime import (
    ContextItem,
    ContextPolicyEngine,
    ContextRequest,
    ContextSelectionResult,
    ContextViewPolicy,
)
from agent_runtime.errors import ContextBuildBlockedError, ContextTooLargeError
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
BUDGET_SOURCE = 1500
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

    prepare_request 是主循环的调用前边界，含最终编码、预算与 manifest。
    Plan 路径先填必需内容，再按节点阶段弹性分配可选段。
    普通 L1 sections（填充顺序）:
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
        "source",
        "feedback",
        "history",
        "state",
    )
    NATIVE_SYSTEM_ORDER = ("system", "tools")
    DYNAMIC_ORDER = (
        "skills",
        "workspace",
        "memory",
        "knowledge",
        "source",
        "feedback",
        "history",
        "state",
    )

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

    def _check_hard_cap(self, used: int, metadata: dict | None = None) -> None:
        """检查总 token 数是否超出硬顶；超出则抛 ContextTooLargeError。"""
        if used > self.hard_cap:
            meta = dict(metadata or {})
            sections = dict(meta.get("sections") or {})
            offenders = sorted(sections.items(), key=lambda item: int(item[1] or 0), reverse=True)
            raise ContextTooLargeError(
                actual=used,
                limit=self.hard_cap,
                metadata={
                    "sections": sections,
                    "largest_sections": offenders[:5],
                    "cuts": list(meta.get("cuts") or [])[-12:],
                },
            )

    def build(self, user_message: str) -> tuple[str, dict]:
        """组装完整 prompt，返回 (prompt_text, metadata)。

        metadata 含各 section token 数、裁剪日志和 prompt_cache_key。

        Raises:
            ContextTooLargeError: 若合计 tokens 超出硬顶限制。
        """
        metadata = self._base_metadata()
        sections = self._fill_sections(user_message, metadata)
        self._check_hard_cap(metadata.get("total_tokens", 0), metadata)
        result_parts = [sections[name] for name in self.SECTION_ORDER if sections.get(name)]
        if sections.get("request"):
            result_parts.append(sections["request"])
        text = "\n".join(result_parts)
        if metadata.get("required_state_ref"):
            actual = self.budget.count(text)
            limit = min(self.budget.total_limit, self.hard_cap)
            if actual > limit:
                raise ContextBuildBlockedError(
                    "context_required_over_budget",
                    metadata={"actual_tokens": actual, "available_tokens": limit},
                )
            metadata["provider_input_tokens"] = actual
        return text, metadata

    def build_dynamic_context(self, user_message: str) -> tuple[str, dict]:
        """组装动态上下文（不含 system / request）。

        Raises:
            ContextTooLargeError: 若合计 tokens 超出硬顶限制。
        """
        metadata = self._base_metadata()
        sections = self._fill_sections(
            user_message, metadata, include_system=False, include_request=False
        )
        self._check_hard_cap(metadata.get("total_tokens", 0), metadata)
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
        self._check_hard_cap(metadata.get("total_tokens", 0), metadata)
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

    def prepare_request(
        self,
        user_message,
        *,
        protocol,
        tools=(),
        native_tail=(),
        tail_refs=None,
        directives=(),
        action_required=False,
        tool_choice=None,
        max_output_tokens=4096,
        deadline=None,
        user_override=None,
    ):
        """One model request boundary: select, encode, fit, validate and seal."""
        from agent_runtime.context_preparation import native_groups, project_tail, request_hash
        from agent_runtime.model_turn import ModelTurnRequest

        if protocol not in {"xml", "native"}:
            raise ValueError("unknown_context_protocol")
        tools = copy.deepcopy(list(tools))
        all_groups = native_groups(list(native_tail)) if protocol == "native" else []
        groups = [] if action_required else all_groups
        # Keep at most the recent three complete groups, never slice messages.
        groups = groups[-3:]
        metadata = self._base_metadata()
        protocol_tokens = (
            self.budget.count(json.dumps(tools, ensure_ascii=False)) + 64
            if protocol == "native"
            else 32
        )
        sections = self._fill_sections(
            user_message,
            metadata,
            native_tools=protocol == "native",
            protocol_tokens=protocol_tokens,
            directives="\n\n".join(filter(None, directives)),
            tail_groups=groups,
            tail_refs=tail_refs,
        )
        governed = bool(metadata.get("required_state_ref"))
        if user_override is not None:
            if governed:
                raise ContextBuildBlockedError("context_required_overwrite")
            for name in self.DYNAMIC_ORDER:
                sections.pop(name, None)
                metadata["sections"][name] = 0
            sections["request"] = user_override
            metadata["sections"]["request"] = self.budget.count(user_override)
        self._check_hard_cap(metadata["total_tokens"], metadata)
        chosen_tail = (
            getattr(self, "_selected_tail", [])
            if governed
            else project_tail(groups, tail_refs or {}, metadata)
        )
        chosen_ids = {
            block["id"]
            for group in chosen_tail
            for block in group[0]["content"]
            if block.get("type") == "tool_use"
        }
        retained = [
            group
            for group in groups
            if any(block.get("id") in chosen_ids for block in group[0]["content"])
        ]

        def encode():
            if protocol == "xml":
                text = "\n".join(
                    sections[name] for name in self.SECTION_ORDER if sections.get(name)
                )
                text += "\n" + sections.get("request", "")
                return ModelTurnRequest(
                    "",
                    [{"role": "user", "content": text}],
                    max_output_tokens=max_output_tokens,
                    deadline=deadline,
                )
            system = "\n\n".join(
                sections[name] for name in self.NATIVE_SYSTEM_ORDER if sections.get(name)
            )
            user = "\n\n".join(
                sections[name] for name in (*self.DYNAMIC_ORDER, "request") if sections.get(name)
            )
            projected = project_tail(retained, tail_refs or {}, metadata)
            messages = [
                {"role": "user", "content": user},
                *[message for group in projected for message in group],
            ]
            return ModelTurnRequest(
                system, messages, tools, tool_choice, max_output_tokens, deadline
            )

        def count(request):
            if protocol == "xml":
                return self.budget.count(request.messages[0]["content"])
            return self.budget.count(request.system_prompt) + self.budget.count(
                json.dumps(
                    {"messages": request.messages, "tools": request.tools}, ensure_ascii=False
                )
            )

        limit = min(self.budget.total_limit, self.hard_cap, self.agent.config.prompt_budget)
        request = encode()
        actual = count(request)
        if governed:
            # Protocol encoding may add a few tokens. Drop lowest-priority whole
            # optional sections; required sections never enter this list.
            for name in reversed(metadata["elastic_budget"]["priority"]):
                if actual <= limit:
                    break
                if not sections.get(name):
                    continue
                sections.pop(name)
                metadata["sections"][name] = 0
                metadata["cuts"].append(f"elastic:{name}:protocol_budget")
                metadata["elastic_budget"]["allocations"][name]["reason"] = "protocol_budget"
                if name == "tail":
                    retained = []
                elif name == "source":
                    metadata["_source_observation_refs"] = []
                    selection = metadata.get("source_selection", {})
                    lost = selection.get("selected_ids", [])
                    metadata["_selected_context_ids"] = [
                        item
                        for item in metadata.get("_selected_context_ids", [])
                        if item not in lost
                    ]
                    metadata.setdefault("_dropped_context_ids", []).extend(lost)
                    selection["selected_ids"] = []
                    selection["selected_items"] = []
                    selection["used_tokens"] = 0
                    selection.setdefault("dropped", []).extend(
                        {"item_id": item, "reason": "protocol_budget", "utility": 0}
                        for item in lost
                    )
                    metadata["_context_selection"] = copy.deepcopy(selection)
                elif name == "feedback":
                    metadata["feedback_observation_refs"] = []
                request = encode()
                actual = count(request)
            if actual > limit:
                raise ContextBuildBlockedError(
                    "context_required_over_budget",
                    metadata={"actual_tokens": actual, "available_tokens": limit},
                )
            self.agent._plan_session.validate_required_context(metadata["long_task_context"])
        projected = project_tail(retained, tail_refs or {}, metadata)
        if retained:
            sections["tail"] = json.dumps(projected, ensure_ascii=False)
        for name, allocation in metadata.get("elastic_budget", {}).get("allocations", {}).items():
            allocation["used_tokens"] = self.budget.count(sections.get(name, ""))
            allocation["borrowed_tokens"] = max(
                0, allocation["used_tokens"] - allocation["soft_quota"]
            )
            allocation["released_tokens"] = max(
                0, allocation["soft_quota"] - allocation["used_tokens"]
            )
            metadata["sections"][name] = allocation["used_tokens"]
        if governed:
            elastic = metadata["elastic_budget"]
            elastic["unused_tokens"] = elastic["pool_tokens"] - sum(
                item["used_tokens"] for item in elastic["allocations"].values()
            )
        metadata["provider_input_tokens"] = actual
        metadata["total_tokens"] = actual
        metadata["request_protocol"] = protocol
        metadata["request_hash"] = request_hash(request, protocol)
        metadata["selected_tool_call_ids"] = [
            block["id"]
            for group in projected
            for block in group[0]["content"]
            if block.get("type") == "tool_use"
        ]
        metadata["dropped_tool_call_ids"] = [
            block["id"]
            for group in all_groups
            for block in group[0]["content"]
            if block.get("type") == "tool_use"
            and block["id"] not in metadata["selected_tool_call_ids"]
        ]
        eligible_ids = {
            block["id"]
            for group in groups
            for block in group[0]["content"]
            if block.get("type") == "tool_use"
        }
        metadata["tool_tail_drops"] = [
            {
                "call_id": call_id,
                "reason": (
                    "forced_action"
                    if action_required
                    else "recency_limit"
                    if call_id not in eligible_ids
                    else "protocol_budget"
                    if "elastic:tail:protocol_budget" in metadata["cuts"]
                    else "elastic_budget"
                ),
            }
            for call_id in metadata["dropped_tool_call_ids"]
        ]
        if governed:
            from agent_runtime.plan_runtime.evidence_view import consumption_manifest

            metadata["evidence_consumption"] = consumption_manifest(
                metadata["long_task_context"],
                metadata,
                sections,
                tail=projected,
                tail_refs=tail_refs,
            )
        metadata["sections"]["prefix"] = sum(
            metadata["sections"].get(name, 0) for name in ("system", "tools", "skills", "workspace")
        )
        metadata["_context_prefix_text"] = "\n".join(
            sections[name] for name in self.SECTION_ORDER if sections.get(name)
        )
        attach_context_projection(metadata, agent=self.agent, budget=self.budget)
        self._record_context_manifest(metadata, sections)
        history = self.agent.read_history()
        seal_history_at_build(self.agent.session, len(history), sections.get("history", ""))
        return request, metadata

    def validate_prepared_request(self, request, metadata):
        from agent_runtime.context_preparation import request_hash

        if metadata["request_hash"] != request_hash(request, metadata["request_protocol"]):
            raise ContextBuildBlockedError("context_request_changed")
        if metadata.get("required_state_ref"):
            self.agent._plan_session.validate_required_context(metadata["long_task_context"])

    def _fill_sections(
        self,
        user_message: str,
        metadata: dict,
        *,
        include_system: bool = True,
        include_request: bool = True,
        native_tools: bool = False,
        protocol_tokens: int = 0,
        directives: str = "",
        tail_groups=None,
        tail_refs=None,
    ) -> dict[str, str]:
        """按预算填充各 section，返回 name → 文本。"""
        total = self.budget.total_limit
        governed = getattr(self.agent, "_plan_session", None) is not None
        state_text = self._get_long_task(metadata) if governed else self._get_state()
        if governed and not native_tools:
            # Reserve delimiters before optional filling, then count the joined
            # XML prompt too. No mandatory text is cut to make room.
            protocol_tokens = 32
        if governed:
            total = max(0, min(total, self.hard_cap) - protocol_tokens)
        request_text = ""
        request_tokens = 0
        section_cap = total

        if include_request:
            processed = (
                user_message if governed else apply_l1_to_request_text(user_message, self.budget)
            )
            request_text, tpl_meta = render_task_message(
                processed,
                repo_root=self._agent_repo_root(),
            )
            metadata.update(tpl_meta)
            if directives:
                request_text += "\n\n" + directives
            request_tokens = self.budget.count(request_text)
            if request_tokens > total and not governed:
                original_tokens = request_tokens
                target = max(256, int(total * 0.60))
                request_text = self._compact_oversized_request(request_text, target)
                request_tokens = self.budget.count(request_text)
                metadata["emergency_compaction"] = {
                    "reason": "request_exceeds_prompt_budget",
                    "original_request_tokens": original_tokens,
                    "compacted_request_tokens": request_tokens,
                    "target_tokens": target,
                    "preserved": ["request_head", "request_tail"],
                }
                metadata["cuts"].append(
                    f"紧急压缩 request: {original_tokens} -> {request_tokens} tokens"
                )
            metadata["sections"]["request"] = request_tokens
            metadata.update(task_preservation_metadata(request_tokens, total))
            if metadata.get("emergency_compaction"):
                metadata["request_preserved"] = False
                metadata["task_budget_overflow"] = True
            section_cap = total if governed else reserve_section_budget(total, request_tokens)

        filler = SectionFiller(
            self.budget,
            metadata,
            section_cap=section_cap,
            total_limit=total,
            scaled_budget=scaled_section_budget,
        )
        self._active_metadata = metadata
        self._prepare_code_evidence(metadata, (tail_refs or {}).values())

        if governed:
            required = {"state": state_text}
            if include_request:
                required["request"] = request_text
            if include_system:
                required["system"] = (
                    self._get_system_for_native() if native_tools else self._get_system()
                )
                # Native schemas are reserved and supplied through the provider
                # protocol; repeating their text here wastes protected budget.
                if not native_tools:
                    required["tools"] = self._get_tools()
                # Role instructions are mandatory. Generic invocation examples
                # are redundant with the rules/schemas and omitted on Plan runs.
                required["skills"] = getattr(self.agent._prefix, "role_text", "") or ""
            filler.add_required(required)
            metadata["protocol_reserved_tokens"] = protocol_tokens
        elif include_system:
            if native_tools:
                filler.add_stable_section("system", self._get_system_for_native(), BUDGET_SYSTEM)
                filler.add_stable_section("tools", self._get_tools(), BUDGET_TOOLS)
                filler.add_stable_section("skills", self._get_skills_for_native(), BUDGET_SKILLS)
            else:
                filler.add_stable_section("system", self._get_system(), BUDGET_SYSTEM)
                filler.add_stable_section("tools", self._get_tools(), BUDGET_TOOLS)
                filler.add_stable_section("skills", self._get_skills(), BUDGET_SKILLS)
        if governed:
            self._fill_elastic_sections(
                filler, metadata, user_message, tail_groups or [], tail_refs or {}
            )
        else:
            self._fill_legacy_sections(
                filler, metadata, user_message, section_cap, total, state_text
            )

        sections = dict(filler.sections)
        used = filler.used

        if include_request:
            sections["request"] = request_text
            if not governed:
                used += request_tokens

        metadata["_context_prefix_text"] = "\n".join(
            sections[name] for name in self.SECTION_ORDER if sections.get(name)
        )
        prefix_names = ("system", "tools", "skills", "workspace")
        metadata["sections"]["prefix"] = sum(
            metadata["sections"].get(name, 0) for name in prefix_names
        )
        metadata["total_tokens"] = used
        metadata["budget"] = self.budget.total_limit
        if governed:
            self.agent._plan_session.validate_required_context(metadata["long_task_context"])
        metadata["tokenizer_backend"] = self.budget.backend
        attach_context_projection(metadata, agent=self.agent, budget=self.budget)
        self._record_context_manifest(metadata, sections)
        history = self.agent.read_history()
        if history and sections.get("history"):
            seal_history_at_build(self.agent.session, len(history), sections["history"])
        return sections

    def _fill_legacy_sections(self, filler, metadata, user_message, section_cap, total, state_text):
        """Keep the established standalone L1 context contract."""
        filler.add_section(
            "workspace",
            self._get_workspace(),
            scaled_section_budget(BUDGET_PREFIX, section_cap or total),
        )
        filler.add_section("state", state_text, 200)
        degradation = self.agent.session.get("runtime_degradation", {}) or {}
        if degradation.get("skip_optional_context"):
            metadata.setdefault("cuts", []).append("degradation:optional_context")
        else:
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
            available = max(0, min(BUDGET_SOURCE, section_cap - filler.used))
            filler.add_section("source", self._get_source(metadata, available), BUDGET_SOURCE)
        # Keep the current request available to the integrity check.  The
        # request is intentionally not part of projected history, so checking
        # the goal against history alone incorrectly reports goal loss on every
        # native build.
        metadata["_context_issue"] = user_message
        history_text = self._get_compressed_history(metadata)
        for observation_id in metadata.get("_source_observation_refs", []):
            pattern = (
                rf"(?ms)^\*\*tool\*\*: \[{re.escape(observation_id)}\].*?"
                r"(?=^\*\*(?:user|assistant|tool|system)\*\*:|\Z)"
            )
            history_text = re.sub(
                pattern,
                f"**tool**: [source selected in current context: {observation_id}]\n",
                history_text,
            )
        metadata["_history_section_text"] = history_text
        filler.add_section(
            "history",
            history_text,
            history_window_budget(section_cap or total),
        )

    def _fill_elastic_sections(self, filler, metadata, user_message, groups, refs):
        from dataclasses import replace

        from agent_runtime.context_preparation import pack_units, project_tail
        from agent_runtime.context_runtime import ContextDecision

        pool = max(0, filler.section_cap - filler.used)
        kind = metadata["long_task_context"]["current_node"]["kind"]
        priority = (
            ["tail", "feedback", "source"] if kind == "verify" else ["source", "tail", "feedback"]
        ) + ["history", "memory", "knowledge", "workspace"]
        weights = {
            "source": 50,
            "tail": 20,
            "feedback": 20,
            "history": 4,
            "memory": 2,
            "knowledge": 2,
            "workspace": 2,
        }
        if kind == "verify":
            weights.update(source=20, tail=35, feedback=35)
        self._source_selection = None
        self._selected_source = []
        self._selected_tail = []
        self._selected_feedback = []
        candidates = {}
        fitters = {}
        if pool:
            candidates["workspace"] = self._get_workspace()
            skip = (self.agent.session.get("runtime_degradation") or {}).get(
                "skip_optional_context"
            )
            if not skip:
                candidates["memory"] = self._get_memory()
                candidates["knowledge"] = self._get_knowledge(user_message)
                candidates["source"] = self._get_source(metadata, pool, elastic=True)
            feedback = []
            for item in (
                []
                if groups
                else [item for item in self.agent.read_history() if item.get("role") == "tool"][-3:]
            ):
                oid = str(item.get("observation_id", ""))
                view = metadata.get("code_evidence", {}).get(oid)
                content = (
                    view.get("content", view.get("diagnostic", ""))
                    if view
                    else str(item.get("content", ""))
                )
                feedback.append((oid, f"[recent_tool_feedback observation_ref={oid}]\n{content}"))

            def render_feedback(units):
                return "\n\n".join(text for _, text in units)

            def fit_feedback(_text, limit):
                self._selected_feedback = pack_units(
                    feedback, limit, self.budget, render_feedback, newest=True
                )
                return render_feedback(self._selected_feedback)

            candidates["feedback"] = render_feedback(feedback)
            fitters["feedback"] = fit_feedback

            def render_tail(units):
                return json.dumps(units, ensure_ascii=False) if units else ""

            projected = project_tail(groups, refs, metadata)

            def fit_tail(_text, limit):
                self._selected_tail = pack_units(
                    projected, limit, self.budget, render_tail, newest=True
                )
                return render_tail(self._selected_tail)

            candidates["tail"] = render_tail(projected)
            fitters["tail"] = fit_tail
            if self._source_selection:
                source_items = self._source_selection.selected

                def render_source(units):
                    return (
                        ("## 当前代码片段\n" + "\n\n".join(item.content for item in units))
                        if units
                        else ""
                    )

                def fit_source(_text, limit):
                    self._selected_source = pack_units(
                        source_items, limit, self.budget, render_source
                    )
                    return render_source(self._selected_source)

                fitters["source"] = fit_source
            candidates["history"] = self._get_compressed_history(metadata)
        filler.add_elastic(candidates, weights, priority, fitters=fitters)
        if self._source_selection:
            selection = self._source_selection
            chosen = {item.item_id for item in self._selected_source}
            selection = replace(
                selection,
                selected=self._selected_source,
                used_tokens=sum(item.token_cost for item in self._selected_source),
                dropped=[
                    *selection.dropped,
                    *(
                        ContextDecision(item.item_id, "elastic_budget")
                        for item in selection.selected
                        if item.item_id not in chosen
                    ),
                ],
            )
            self._record_selection_result(metadata, selection)
            metadata["source_selection"] = selection.to_dict()
            metadata["_source_observation_refs"] = [item.source_ref for item in selection.selected]
        metadata["feedback_observation_refs"] = [oid for oid, _ in self._selected_feedback if oid]
        history = filler.sections.get("history", "")
        for oid in set(
            metadata.get("_source_observation_refs", []) + metadata["feedback_observation_refs"]
        ):
            history = re.sub(
                rf"(?ms)^\*\*tool\*\*: \[{re.escape(oid)}\].*?"
                r"(?=^\*\*(?:user|assistant|tool|system)\*\*:|\Z)",
                f"**tool**: [source selected in current context: {oid}]\n",
                history,
            )
        if "history" in filler.sections:
            old = metadata["sections"]["history"]
            filler.sections["history"] = history
            metadata["sections"]["history"] = self.budget.count(history)
            filler.used += metadata["sections"]["history"] - old
            metadata["elastic_budget"]["unused_tokens"] += old - metadata["sections"]["history"]
            metadata["elastic_budget"]["allocations"]["history"]["used_tokens"] = metadata[
                "sections"
            ]["history"]

    def _get_source(self, metadata: dict, token_limit: int, *, elastic: bool = False) -> str:
        context = getattr(self.agent, "tool_context", None)
        service = getattr(context, "exploration_service", None)
        if service is None or getattr(service, "mode", "") != "relations":
            return ""
        from agent_runtime.code_exploration.context import select_source_context

        text, selection = select_source_context(
            service,
            self.budget,
            role=str(getattr(self.agent, "agent_name", "") or ""),
            phase=str(getattr(self.agent, "_l2_phase", "repair") or "repair"),
            token_limit=max(0, token_limit - 16),
            source_checks=getattr(self, "_code_source_checks", None),
            elastic=elastic,
        )
        if selection is not None:
            if elastic:
                self._source_selection = selection
            else:
                self._record_selection_result(metadata, selection)
                metadata["source_selection"] = selection.to_dict()
                metadata["_source_observation_refs"] = [
                    item.source_ref for item in selection.selected
                ]
            metadata["source_epoch"] = service.epoch
        return text

    def _prepare_code_evidence(self, metadata: dict, tail_refs=()) -> None:
        """Validate once per build, sharing bounded I/O across history and tail."""
        from agent_runtime.code_exploration.consumption import (
            SourceChecks,
            retrieval_header,
            unavailable_evidence,
        )
        from agent_runtime.context_runtime import ObservationStore

        context = getattr(self.agent, "tool_context", None)
        if context is None or not getattr(context, "root", ""):
            return
        checks = SourceChecks(context)
        self._code_source_checks = checks
        store = ObservationStore(self.agent.session, context.root, context.state_root)
        views = metadata.setdefault("code_evidence", {})
        blocked_paths = set()
        try:
            references = {str(item.get("observation_id", "")) for item in self.agent.read_history()}
            references.update(str(oid) for oid in tail_refs)
            references.update(
                str(item.get("observation_id", ""))
                for item in self.agent.session.get("tool_observations", [])[-50:]
            )
            records = sorted(
                (
                    record
                    for oid in references
                    if (record := store.get(oid)) and record.retrieval_result
                ),
                key=lambda record: record.created_at,
                reverse=True,
            )
            for record in records:
                oid = record.observation_id
                result = store.expand_for_context(
                    oid,
                    max_tokens=8000,
                    context=context,
                    source_checks=checks,
                    actor="model_context",
                )
                failure = next(
                    (
                        str(fact.get("error_code", ""))
                        for fact in reversed(record.structured_facts)
                        if fact.get("error_code")
                    ),
                    record.error_code,
                )
                receipt = f"[tool_status={record.status} error_code={failure[:80]}]\n"
                if result.get("ok"):
                    content = f"[{oid}] " + retrieval_header(
                        record.retrieval_result, result["freshness"]
                    )
                    content += result["content"]
                    if result["output_truncated"]:
                        content += f"\n[output_truncated=true observation_id={oid}]"
                    views[oid] = {
                        "ok": True,
                        "freshness": result["freshness"],
                        "content": content + "\n" + receipt,
                        "body": result["content"],
                    }
                else:
                    reason = result.get("reason", "unknown")
                    freshness = result.get("freshness", "stale")
                    views[oid] = {
                        "ok": False,
                        "reason": reason,
                        "freshness": freshness,
                        "diagnostic": receipt
                        + retrieval_header(record.retrieval_result, freshness)
                        + unavailable_evidence(oid, reason, freshness),
                    }
                    blocked_paths.update(record.dependencies)
            metadata["code_evidence_checks"] = {
                "bytes_read": checks.bytes_read,
                "files_checked": checks.checked_files,
            }
            metadata["_blocked_code_paths"] = sorted(blocked_paths)
            if any(not view["ok"] for view in views.values()):
                seal_history_at_build(self.agent.session, 0, "")
        finally:
            store.close()

    def _compact_oversized_request(self, text: str, target_tokens: int) -> str:
        """Keep the issue head and runtime/feedback tail in one deterministic pass."""
        marker = "\n\n[... oversized request compacted ...]\n\n"
        marker_tokens = self.budget.count(marker)
        available = max(1, target_tokens - marker_tokens)
        head_budget = max(1, int(available * 0.75))
        tail_budget = max(1, available - head_budget)
        head = self.budget.fit(text, head_budget).rstrip()

        # TokenBudget.fit is prefix-oriented. Binary search the shortest suffix
        # boundary that fits the reserved tail budget.
        lo, hi = 0, len(text)
        while lo < hi:
            mid = (lo + hi) // 2
            if self.budget.count(text[mid:]) <= tail_budget:
                hi = mid
            else:
                lo = mid + 1
        tail = text[lo:].lstrip()
        compacted = f"{head}{marker}{tail}"
        if self.budget.count(compacted) > target_tokens:
            compacted = self.budget.fit(compacted, target_tokens)
        return compacted

    def _record_context_manifest(self, metadata: dict, sections: dict[str, str]) -> None:
        """Persist the exact dynamic selection needed for trace/checkpoint replay."""
        selected = list(dict.fromkeys(metadata.get("_selected_context_ids", []) or []))
        dropped = list(dict.fromkeys(metadata.get("_dropped_context_ids", []) or []))
        selection = metadata.get("_context_selection", {}) or {}
        canonical = {
            "context_sections": metadata.get("context_sections", {}),
            "selected_context_ids": selected,
            "policy_version": selection.get("policy_version", ContextPolicyEngine.VERSION),
            "sections": metadata.get("sections", {}),
            "section_hashes": {
                name: hashlib.sha256(text.encode()).hexdigest() for name, text in sections.items()
            },
            "required_state_ref": metadata.get("required_state_ref", {}),
            "decision_refs": copy.deepcopy(metadata.get("decision_refs", [])),
            "request_hash": metadata.get("request_hash", ""),
            "request_protocol": metadata.get("request_protocol", ""),
            "code_evidence": {
                oid: {key: view[key] for key in ("ok", "freshness", "reason") if key in view}
                for oid, view in metadata.get("code_evidence", {}).items()
            },
        }
        projection_hash = hashlib.sha256(
            json.dumps(canonical, sort_keys=True, ensure_ascii=True).encode("utf-8")
        ).hexdigest()[:24]
        manifest = {
            "schema_version": "context-v2",
            "projection_hash": projection_hash,
            "policy_version": selection.get("policy_version", ContextPolicyEngine.VERSION),
            "selected_context_ids": selected,
            "dropped_context_ids": dropped,
            "selection": selection,
            "observation_refs": list(metadata.get("_observation_refs", []) or []),
            "total_tokens": int(metadata.get("total_tokens", 0) or 0),
            "budget": int(metadata.get("budget", self.budget.total_limit) or 0),
            "code_evidence": canonical["code_evidence"],
            "code_evidence_checks": metadata.get("code_evidence_checks", {}),
            "required_state_ref": canonical["required_state_ref"],
            "decision_refs": canonical["decision_refs"],
            "required_sections": metadata.get("required_sections", {}),
            "protocol_reserved_tokens": metadata.get("protocol_reserved_tokens", 0),
            "section_hashes": canonical["section_hashes"],
            "stage": "prepared" if metadata.get("request_hash") else "sections",
            "request_hash": canonical["request_hash"],
            "request_protocol": canonical["request_protocol"],
            "provider_input_tokens": metadata.get("provider_input_tokens", 0),
            "elastic_budget": copy.deepcopy(metadata.get("elastic_budget", {})),
            "source_observation_refs": list(metadata.get("_source_observation_refs", [])),
            "feedback_observation_refs": list(metadata.get("feedback_observation_refs", [])),
            "selected_tool_call_ids": list(metadata.get("selected_tool_call_ids", [])),
            "dropped_tool_call_ids": list(metadata.get("dropped_tool_call_ids", [])),
            "tool_tail_drops": copy.deepcopy(metadata.get("tool_tail_drops", [])),
            "evidence_consumption": copy.deepcopy(metadata.get("evidence_consumption", [])),
        }
        metadata["context_manifest"] = manifest
        metadata["context_policy_version"] = manifest["policy_version"]
        metadata["selected_context_ids"] = selected[-100:]
        metadata["dropped_context_ids"] = dropped[-100:]
        metadata["observation_refs"] = list(metadata.get("_observation_refs", []) or [])[-100:]
        self.agent.session["context_manifest"] = manifest
        self.agent.session.setdefault("memory", {})["context_manifest"] = manifest

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
        blocked = set(getattr(self, "_active_metadata", {}).get("_blocked_code_paths", []))
        if blocked:
            mem = copy.deepcopy(mem)
            mem["file_summaries"] = {
                path: info
                for path, info in mem.get("file_summaries", {}).items()
                if path not in blocked
            }
            working_copy = mem.setdefault("working", {})
            working_copy["evidence_ledger"] = [
                {**item, "summary": "source requires reread", "stale": True}
                if item.get("path") in blocked
                else item
                for item in working_copy.get("evidence_ledger", [])
            ]
        working = mem.get("working", {})
        getattr(self, "_active_metadata", {})["_memory_state"] = mem
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

    def _get_long_task(self, metadata: dict) -> str:
        """Project PlanSession state into a compression-protected section."""
        plan_session = getattr(self.agent, "_plan_session", None)
        if plan_session is None:
            return ""
        try:
            context = plan_session.build_required_context(str(metadata.get("plan_node_id", "")))
        except (ValueError, OSError):
            raise ContextBuildBlockedError("state_mismatch") from None
        metadata["long_task_context"] = context
        metadata["decision_refs"] = [
            {key: check[key] for key in ("decision_id", "revision", "checksum", "status")}
            for check in context.get("decision_checks", [])
        ]
        self.agent.session["long_task_context"] = context
        view = context["plan_view"]
        metadata["required_state_ref"] = {
            key: view[key]
            for key in (
                "task_id",
                "run_id",
                "workspace_id",
                "plan_id",
                "plan_version",
                "state_revision",
                "plan_checksum",
            )
        }
        metadata["required_state_ref"].update(
            node_id=context["current_node"]["node_id"],
            task_state_revision=context["state_revision"],
            state_checksum=context["state_checksum"],
        )
        compact = {
            key: value
            for key, value in context.items()
            if key not in {"plan_view", "evidence_checks", "decision_checks"}
        }
        compact["plan_ref"] = metadata["required_state_ref"]
        return "长任务状态（必需内容，压缩保护）:\n" + json.dumps(
            compact, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        )

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
            role=str(getattr(self.agent, "_l2_agent", "") or ""),
            token_budget=scaled_section_budget(BUDGET_KNOWLEDGE, self.budget.total_limit),
            min_kind_counts={"memory": 1},
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
            record_event=False,
        )
        recalled_ids = [str(item.get("memory_id", "")) for item in governed_results]
        turn_id = str(mem.get("_turn_counter") or self.agent.session.get("_turn_counter", ""))
        prompt_id = hashlib.sha256(
            f"{turn_id}|{query}".encode("utf-8", errors="replace")
        ).hexdigest()[:16]
        if governed_results:
            governed_items = [
                ContextItem(
                    item_id=item["memory_id"],
                    kind="memory",
                    content=str(item.get("value", "")),
                    source_ref=item["memory_id"],
                    token_cost=max(1, self.budget.count(str(item.get("value", "")))),
                    relevance=float(item.get("score", 0.0)),
                    confidence=float(item.get("confidence", 0.0)),
                    freshness=1.0,
                    evidence_strength=1.0 if item.get("evidence_refs") else 0.0,
                    scope=str(item.get("scope", "task")),
                )
                for item in governed_results
            ]
            view = ContextViewPolicy.for_request(request)
            governed_items = [item for item in governed_items if view.allows(item)]
            governed_result = policy.select_with_result(governed_items, request)
            governed_items = governed_result.selected
            self._record_selection_result(metadata=None, result=governed_result)
            mem["recalled_memory_ids"] = [item.source_ref for item in governed_items]
            for item in governed_items:
                governed.record_usage_stage(
                    item.source_ref,
                    usage="projected",
                    stage=str(getattr(self.agent, "_l2_phase", "repair") or "repair"),
                    task_id=str(identity.get("task_id", "") or ""),
                    turn_id=turn_id,
                    prompt_id=prompt_id,
                    context_item_id=item.item_id,
                    evidence_refs=list(
                        governed.inspect(item.source_ref).get("evidence_refs", [])
                        if governed.inspect(item.source_ref)
                        else []
                    ),
                    decision_reason="context_policy_selected",
                )
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
                recalled_ids.extend(str(item.get("memory_id", "")) for item in durable_results)
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
        mem["memory_context_attribution"] = {
            "turn_id": turn_id,
            "prompt_id": prompt_id,
            "phase": str(getattr(self.agent, "_l2_phase", "repair") or "repair"),
        }
        governed.record_recall(
            recalled_ids,
            task_id=str(identity.get("task_id", "") or ""),
            stage=str(getattr(self.agent, "_l2_phase", "repair") or "repair"),
            turn_id=turn_id,
            prompt_id=prompt_id,
            decisions=list(mem.get("memory_recall_decisions", [])),
        )
        return "\n".join(parts) if parts else ""

    def _record_selection_result(
        self, metadata: dict | None, result: ContextSelectionResult
    ) -> None:
        """Accumulate selection decisions on session and current build metadata."""
        target = metadata if metadata is not None else getattr(self, "_active_metadata", {})
        selected = target.setdefault("_selected_context_ids", [])
        dropped = target.setdefault("_dropped_context_ids", [])
        selected.extend(result.selected_ids)
        dropped.extend(result.dropped_ids)
        target["_context_selection"] = result.to_dict()
        session = self.agent.session
        session.setdefault("context_selection_history", []).append(result.to_dict())
        session["context_selection_history"] = session["context_selection_history"][-50:]

    def _get_compressed_history(self, metadata: dict | None = None) -> str:
        """获取压缩后的对话历史（L0–L5 管线；已封印段单调追加）。

        优先从 .agent/history.jsonl 读取，文件缺失时回退 session 内存。
        ContextManager 不写入 JSONL（写路径由 AgentLoop.record 独占）。
        """
        history = self.agent.read_history()
        if not history:
            return ""
        views = (metadata or {}).get("code_evidence", {})
        history = [
            {**item, "content": view.get("content", view.get("diagnostic", ""))}
            if item.get("role") == "tool" and (view := views.get(item.get("observation_id")))
            else item
            for item in history
        ]

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
        meta.setdefault("_memory_state", self.agent.session.get("memory", {}))
        sealed_count, sealed_text = get_sealed_history(self.agent.session)
        include_header = not (sealed_count > 0 and sealed_text)

        projected = run_compression_pipeline(
            history,
            self.budget,
            metadata=meta,
            summarizer=make_summarizer(self.agent),
            summary_cache=(
                {}
                if any(not view["ok"] for view in meta.get("code_evidence", {}).values())
                else self._summary_cache
            ),
            history_window=history_window_budget(self.budget.total_limit),
            tier_policy=self.tier_policy,
        )
        source_refs = set(meta.get("_source_observation_refs", []))
        if source_refs:
            redacted = []
            for item in projected:
                if item.get("role") != "tool":
                    redacted.append(item)
                    continue
                content = str(item.get("content", ""))
                reference = next(
                    (
                        oid
                        for oid in source_refs
                        if item.get("observation_id") == oid or content.startswith(f"[{oid}]")
                    ),
                    "",
                )
                redacted.append(
                    {**item, "content": f"[source selected in current context: {reference}]"}
                    if reference
                    else item
                )
            projected = redacted
        observation_refs = [
            str(item.get("observation_id")) for item in projected if item.get("observation_id")
        ]
        meta.setdefault("_selected_context_ids", []).extend(observation_refs)
        meta["_observation_refs"] = list(dict.fromkeys(observation_refs))

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
