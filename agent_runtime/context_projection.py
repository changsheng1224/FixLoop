"""八段 Context 投影 schema：实现层 sections → 语义 context_sections。"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from agent_runtime.context_manager import TokenBudget

CONTEXT_SCHEMA_VERSION = 1

EIGHT_SECTIONS = (
    "system",
    "task",
    "state",
    "knowledge",
    "tools",
    "skills",
    "memory",
    "history",
)

TOOLS_MARKER = "## 可用工具"
EXAMPLES_MARKER = "## 调用示例"


def empty_context_sections() -> dict[str, int]:
    """返回八段全 0 占位。"""
    return {name: 0 for name in EIGHT_SECTIONS}


def split_stable_text(stable_text: str) -> tuple[str, str, str]:
    """将 prefix stable 拆为 (core, tools, examples)。"""
    text = stable_text or ""
    tools_idx = text.find(TOOLS_MARKER)
    examples_idx = text.find(EXAMPLES_MARKER)

    if tools_idx == -1:
        if examples_idx == -1:
            return text.strip(), "", ""
        return text[:examples_idx].strip(), "", text[examples_idx:].strip()

    core = text[:tools_idx].strip()
    if examples_idx == -1 or examples_idx < tools_idx:
        return core, text[tools_idx:].strip(), ""

    return core, text[tools_idx:examples_idx].strip(), text[examples_idx:].strip()


def _count_text(budget: TokenBudget | Any, text: str) -> int:
    if not text:
        return 0
    return budget.count(text)


def _reconcile_stable_splits(
    core_t: int,
    tools_t: int,
    examples_t: int,
    stable_impl: int,
) -> tuple[int, int, int]:
    """当 stable 作为整块 fit 后，按比例 reconcile 拆分计数。"""
    split_sum = core_t + tools_t + examples_t
    if stable_impl <= 0 or split_sum <= stable_impl:
        return core_t, tools_t, examples_t

    if split_sum <= 0:
        return 0, 0, 0

    ratio = stable_impl / split_sum
    core_adj = int(core_t * ratio)
    tools_adj = int(tools_t * ratio)
    examples_adj = max(0, stable_impl - core_adj - tools_adj)
    return core_adj, tools_adj, examples_adj


def count_state_section(agent, budget: TokenBudget | Any) -> int:
    """state 段：plan_todos 投影 token（Phase 1 占位）。"""
    session = getattr(agent, "session", None) or {}
    todos = session.get("plan_todos")
    if not todos:
        return 0
    try:
        text = json.dumps(todos, ensure_ascii=False)
    except (TypeError, ValueError):
        text = str(todos)
    return _count_text(budget, text)


def build_context_sections(
    implementation_sections: dict[str, int],
    *,
    agent,
    budget: TokenBudget | Any,
) -> dict[str, int]:
    """五/六 section 实现层 → 八段语义投影。"""
    ctx = empty_context_sections()

    if "tools" in implementation_sections or "skills" in implementation_sections:
        ctx["system"] = int(implementation_sections.get("system", 0) or 0) + int(
            implementation_sections.get("workspace", 0) or 0
        )
        ctx["tools"] = int(implementation_sections.get("tools", 0) or 0)
        ctx["skills"] = int(implementation_sections.get("skills", 0) or 0)
    else:
        prefix = getattr(agent, "_prefix", None)
        stable = getattr(prefix, "stable_text", "") or ""
        core, tools_text, examples_text = split_stable_text(stable)
        core_t = _count_text(budget, core)
        tools_t = _count_text(budget, tools_text)
        examples_t = _count_text(budget, examples_text)
        stable_impl = int(implementation_sections.get("system", 0) or 0)
        core_t, tools_t, examples_t = _reconcile_stable_splits(
            core_t, tools_t, examples_t, stable_impl
        )
        workspace_t = int(implementation_sections.get("workspace", 0) or 0)
        role_t = int(implementation_sections.get("role", 0) or 0)
        ctx["system"] = core_t + workspace_t
        ctx["tools"] = tools_t
        ctx["skills"] = examples_t + role_t

    ctx["memory"] = int(implementation_sections.get("memory", 0) or 0)
    ctx["knowledge"] = int(implementation_sections.get("relevant", 0) or 0)
    ctx["state"] = count_state_section(agent, budget)
    ctx["history"] = int(implementation_sections.get("history", 0) or 0)
    ctx["task"] = int(implementation_sections.get("request", 0) or 0)
    return ctx


def build_context_sections_from_fit(sections: dict[str, int]) -> dict[str, int]:
    """fit_prompt_to_budget 的 system/user → 八段（其余为 0）。"""
    ctx = empty_context_sections()
    ctx["system"] = int(sections.get("system", 0) or 0)
    ctx["task"] = int(sections.get("user", 0) or 0)
    return ctx


def attach_context_projection(
    metadata: dict,
    *,
    agent,
    budget: TokenBudget | Any,
) -> None:
    """写入 metadata.context_sections（保留 legacy sections）。"""
    impl = metadata.get("sections") or {}
    ctx = build_context_sections(impl, agent=agent, budget=budget)
    metadata["context_schema_version"] = CONTEXT_SCHEMA_VERSION
    metadata["context_sections"] = ctx
    metadata["context_sections_total"] = sum(ctx.values())


def attach_fit_context_projection(metadata: dict) -> None:
    """fit_prompt_to_budget 路径的八段投影。"""
    impl = metadata.get("sections") or {}
    ctx = build_context_sections_from_fit(impl)
    metadata["context_schema_version"] = CONTEXT_SCHEMA_VERSION
    metadata["context_sections"] = ctx
    metadata["context_sections_total"] = sum(ctx.values())
