"""探索证据质量：检索 degrade 后仍须有可锚定的嫌疑/测试/片段。"""

from __future__ import annotations

import json
from typing import Any

from src.state import RetrievedContext, RepairPlan, SuspectLocation

__all__ = [
    "explore_has_anchor",
    "explore_quality",
    "has_grounded_impl_for_state",
    "merge_retrieved_context",
    "record_explore_quality",
]


def explore_has_anchor(
    suspects: list[SuspectLocation] | None,
    context: RetrievedContext | None,
    plan: RepairPlan | None,
) -> bool:
    """是否具备进入 Patcher 的最低探索锚点（机制级，不绑题号）。"""
    if suspects:
        return True
    if plan and plan.suspect_files:
        return True
    if context is None:
        return False
    if context.related_tests or context.similar_snippets or context.caller_locations:
        return True
    return False


def explore_quality(
    suspects: list[SuspectLocation] | None,
    context: RetrievedContext | None,
    plan: RepairPlan | None,
    *,
    repo_root: str = "",
) -> dict[str, Any]:
    n_suspects = len(suspects or [])
    n_tests = len(context.related_tests) if context else 0
    n_snippets = len(context.similar_snippets) if context else 0
    n_callers = len(context.caller_locations) if context else 0
    has_plan_files = bool(plan and plan.suspect_files)
    grounded = False
    if repo_root:
        from src.repair.symbol_index import has_grounded_impl_suspect

        grounded = has_grounded_impl_suspect(suspects, repo_root)
    soft_ok = explore_has_anchor(suspects, context, plan)
    return {
        "n_suspects": n_suspects,
        "n_tests": n_tests,
        "n_snippets": n_snippets,
        "n_callers": n_callers,
        "has_plan_files": has_plan_files,
        "grounded_impl": grounded,
        "ok": grounded or soft_ok,
    }


def has_grounded_impl_for_state(state, repo_root: str) -> bool:
    from src.repair.symbol_index import has_grounded_impl_suspect

    return has_grounded_impl_suspect(getattr(state, "suspect_locations", None), repo_root)


def record_explore_quality(
    state,
    suspects: list[SuspectLocation] | None,
    context: RetrievedContext | None,
    *,
    repo_root: str = "",
) -> dict[str, Any]:
    root = repo_root or str(getattr(state, "repo_path", "") or "")
    q = explore_quality(
        suspects,
        context,
        getattr(state, "repair_plan", None),
        repo_root=root,
    )
    state.node_timings["explore_quality"] = q
    if not q.get("grounded_impl") and not q["ok"]:
        state.agent_errors["explore_insufficient"] = (
            "no_suspects_tests_or_snippets"
        )
    elif root and not q.get("grounded_impl"):
        state.agent_errors["localize_weak_grounding"] = "no_grounded_impl_file"
    else:
        state.agent_errors.pop("explore_insufficient", None)
        state.agent_errors.pop("localize_weak_grounding", None)
    return q


def _dedupe_str(items: list[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for item in items:
        s = (item or "").strip()
        if not s or s in seen:
            continue
        seen.add(s)
        out.append(s)
    return out


def _dedupe_obj(items: list) -> list:
    seen: set[str] = set()
    out: list = []
    for item in items:
        if isinstance(item, dict):
            key = json.dumps(item, sort_keys=True, ensure_ascii=False)
        else:
            key = str(item)
        if key in seen:
            continue
        seen.add(key)
        out.append(item)
    return out


def merge_retrieved_context(
    primary: RetrievedContext | None,
    secondary: RetrievedContext | None,
) -> RetrievedContext:
    """合并两路检索结果（去重保序，primary 优先）。"""
    a = primary or RetrievedContext()
    b = secondary or RetrievedContext()
    return RetrievedContext(
        related_tests=_dedupe_str(list(a.related_tests) + list(b.related_tests)),
        caller_locations=_dedupe_str(list(a.caller_locations) + list(b.caller_locations)),
        similar_snippets=_dedupe_obj(list(a.similar_snippets) + list(b.similar_snippets)),
        similar_fixes=_dedupe_obj(list(a.similar_fixes) + list(b.similar_fixes)),
    )
