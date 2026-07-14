"""八段 Context 投影 schema：实现层 sections → 语义 context_sections。"""

from __future__ import annotations

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


def count_state_section(agent, budget: TokenBudget | Any) -> int:
    """state 段：与 ContextManager._get_state() 同格式的 token 估算。"""
    session = getattr(agent, "session", None) or {}
    todos = session.get("plan_todos")
    if not todos:
        return 0

    parts: list[str] = []
    # task_summary
    mem = session.get("memory", {})
    working = mem.get("working", {})
    task_summary = (working.get("task_summary", "") or "").strip()
    if task_summary:
        parts.append(f"任务: {task_summary}")
    # L2 phase
    l2_phase = (getattr(agent, "_l2_phase", "") or "").strip()
    if l2_phase:
        parts.append(f"阶段: {l2_phase}")
    # plan_todos
    if todos:
        total = len(todos)
        done = sum(1 for t in todos if t.get("status") == "done")
        parts.append(f"进度: {done}/{total}")
        status_icon = {
            "done": "+", "in_progress": ">", "pending": "-",
            "blocked": "!", "cancelled": "x",
        }
        for t in todos[:3]:
            icon = status_icon.get(t.get("status", ""), "?")
            content = (t.get("content", "") or "").strip()
            if content:
                parts.append(f"  {icon} {content}")

    if not parts:
        return 0
    text = "\n".join(parts)
    return _count_text(budget, text)


def build_context_sections(
    implementation_sections: dict[str, int],
    *,
    agent,
    budget: TokenBudget | Any,
) -> dict[str, int]:
    """五/六 section 实现层 → 八段语义投影（仅用 impl 计数，无 marker 回退）。"""
    ctx = empty_context_sections()
    impl = implementation_sections or {}

    ctx["system"] = int(impl.get("system", 0) or 0) + int(impl.get("workspace", 0) or 0)
    ctx["tools"] = int(impl.get("tools", 0) or 0)
    ctx["skills"] = int(impl.get("skills", 0) or 0)
    ctx["memory"] = int(impl.get("memory", 0) or 0)
    ctx["knowledge"] = int(implementation_sections.get("knowledge", 0) or 0)
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
