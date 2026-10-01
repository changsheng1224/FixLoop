"""Version-checked source snippets for the existing context policy."""

from __future__ import annotations

from agent_runtime.code_exploration.service import _snapshot
from agent_runtime.context_runtime import (
    ContextItem,
    ContextPolicyEngine,
    ContextRequest,
    ContextViewPolicy,
)
from agent_runtime.sensitive_paths import is_sensitive_path


def select_source_context(
    service,
    budget,
    *,
    role: str,
    phase: str,
    token_limit: int,
    source_checks=None,
    elastic: bool = False,
) -> tuple[str, object | None]:
    """Recheck source and Observation before every prompt projection."""
    if not service.pending_candidates or token_limit <= 0:
        return "", None
    if not service.validate_view(source_checks):
        reason = service.last_invalidation_reason
        return f"Code evidence unavailable: {reason}; reread required.", None
    from agent_runtime.code_exploration.io import _limits

    request = ContextRequest(
        role=role,
        phase=phase,
        token_budget=token_limit if elastic else min(1500, token_limit),
    )
    candidates: list[ContextItem] = []
    for raw in service.pending_candidates[:6]:
        relative = raw["path"]
        expected = raw["content_hash"]
        try:
            path = service.context.resolve(relative)
            if is_sensitive_path(path):
                service.invalidate("source_policy_changed")
                return "", None
            content, actual = _snapshot(path, _limits(service.context).file_hash_bytes)
        except (OSError, ValueError, UnicodeError):
            service.invalidate("source_unavailable")
            return "", None
        if actual != expected:
            service.invalidate("source_changed")
            return "", None
        start = int(raw["start_line"])
        end = int(raw["end_line"])
        key = (relative, actual, start, end)
        excerpt = service.snippet_cache.get(key)
        if excerpt is None:
            excerpt = "\n".join(content.splitlines()[start - 1 : end - 1])
            if not excerpt:
                continue
            service.snippet_cache[key] = excerpt
        source_ref = raw["observation_id"]
        from agent_runtime.code_exploration.consumption import retrieval_header

        evidence = service.evidence[source_ref]
        text = (
            f"{relative}:{start}-{end - 1} [source={source_ref} version={actual[:12]}]\n"
            "[snippet_freshness=fresh; parent query freshness is separate]\n"
            f"{retrieval_header(evidence.retrieval_result)}{excerpt}"
        )
        candidates.append(
            ContextItem(
                item_id=f"source:{service.epoch}:{relative}:{start}:{actual[:12]}",
                kind="source",
                content=text,
                source_ref=source_ref,
                source_version=actual,
                token_cost=max(1, budget.count(text)),
                relevance=1.0,
                confidence=0.9,
                evidence_strength=1.0,
                metadata={
                    "path": relative,
                    "range": [start, end],
                    "reason": raw["reason"],
                    "epoch": service.epoch,
                    "freshness": "fresh",
                    "completeness": evidence.retrieval_result.get("completeness", "unknown"),
                },
            )
        )
    view = ContextViewPolicy.for_request(request)
    candidates = [item for item in candidates if view.allows(item)]
    selection = ContextPolicyEngine().select_with_result(candidates, request)
    if not selection.selected:
        return "", selection
    return "## 当前代码片段\n" + "\n\n".join(item.content for item in selection.selected), selection
