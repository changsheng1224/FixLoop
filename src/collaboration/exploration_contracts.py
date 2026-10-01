"""Trusted limits and scoped contracts for the two read-only exploration kinds."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from agent_runtime.path_safety import resolve_under_root
from agent_runtime.sensitive_paths import is_sensitive_path

KINDS = frozenset({"implementation_location", "related_tests"})
READ_TOOLS = frozenset({"list_files", "grep", "read_file"})
ACTIVE = frozenset({"queued", "running", "worker_lost"})
TERMINAL = frozenset({"completed", "partial", "failed", "cancelled", "timed_out", "stale"})


@dataclass(frozen=True)
class ExplorationLimits:
    model_turns: int = 3
    tool_calls: int = 4
    tokens: int = 24000
    run_tokens: int = 96000
    deadline_s: float = 60
    cleanup_s: float = 2
    max_wait_ms: int = 1000

    def __post_init__(self):
        if not (1 <= self.model_turns <= 3 and 1 <= self.tool_calls <= 4):
            raise ValueError("exploration_limits_may_only_lower_turn_and_tool_caps")
        if not (0 < self.tokens <= 24000 and 0 < self.run_tokens <= 96000):
            raise ValueError("invalid_exploration_token_budget")
        if not (0 < self.deadline_s <= 60 and 0 <= self.cleanup_s <= 2):
            raise ValueError("invalid_exploration_deadline")
        if not (0 <= self.max_wait_ms <= 1000):
            raise ValueError("invalid_exploration_wait_limit")


def safe_path(root: str, raw: str) -> str:
    if not isinstance(raw, str) or not raw.strip():
        raise ValueError("invalid_exploration_scope")
    path = resolve_under_root(root, raw)
    relative = path.relative_to(Path(root).resolve()).as_posix()
    if is_sensitive_path(raw) or is_sensitive_path(path):
        raise ValueError("sensitive_exploration_scope")
    if any(part.startswith(".") for part in Path(relative).parts if part != "."):
        raise ValueError("control_directory_exploration_denied")
    return relative


def in_scope(path: str, scopes: list[str]) -> bool:
    return any(scope == "." or path == scope or path.startswith(scope + "/") for scope in scopes)


def validate_requests(root: str, requests: list) -> list[dict]:
    if not isinstance(requests, list) or not 1 <= len(requests) <= 2:
        raise ValueError("exploration_batch_requires_one_or_two_tasks")
    validated, kinds = [], set()
    for raw in requests:
        if not isinstance(raw, dict) or set(raw) - {
            "kind",
            "question",
            "scope_paths",
            "input_observation_ids",
        }:
            raise ValueError("exploration_untrusted_fields")
        kind, question = raw.get("kind"), raw.get("question")
        if not isinstance(kind, str) or kind not in KINDS or kind in kinds:
            raise ValueError("exploration_kind_invalid_or_duplicate")
        if not isinstance(question, str) or not question.strip() or len(question) > 2000:
            raise ValueError("exploration_question_invalid")
        scopes = raw.get("scope_paths", [])
        refs = raw.get("input_observation_ids", [])
        if not isinstance(scopes, list) or len(scopes) > 8:
            raise ValueError("exploration_scope_invalid")
        if not isinstance(refs, list) or len(refs) > 8 or any(not isinstance(r, str) for r in refs):
            raise ValueError("exploration_input_refs_invalid")
        scopes = list(dict.fromkeys(safe_path(root, p) for p in scopes)) or ["."]
        validated.append({**raw, "scope_paths": scopes, "input_observation_ids": refs})
        kinds.add(kind)
    return validated
