"""Agentic Loop 五段 trace 事件表与序列校验。"""

from __future__ import annotations

from agent_runtime.react_phases import LoopPath

__all__ = [
    "AGENTIC_LOOP_STAGES",
    "CONTEXT_BUILT_PROJECTION_KEYS",
    "LOOP_PATH_SNAPSHOTS",
    "LoopPath",
    "assert_subsequence",
    "normalize_event_name",
    "normalize_trace_events",
    "validate_loop_trace",
]

# context_built payload 可选投影字段（message_projection）
CONTEXT_BUILT_PROJECTION_KEYS = (
    "projection_step",
    "sealed_history_count",
    "prefix_monotonic",
    "prefix_aligned",
    "prefix_fingerprint",
)

# Observe → Context → Model → Tool → Record
AGENTIC_LOOP_STAGES: dict[str, list[str]] = {
    "observe": ["run_started", "step_timeout?"],
    "context": ["context_built"],
    "model": [
        "react_phase:reasoning",
        "model_request_start",
        "model_first_token",
        "model_complete",
        "parse_retry?",
    ],
    "tool": [
        "react_phase:acting",
        "tool_preview",
        "tool_executed",
        "react_phase:observation",
    ],
    "record": ["react_phase:recording"],
}

# 黄金子序列：? 后缀表示可选事件
LOOP_PATH_SNAPSHOTS: dict[str, dict[str, list[str]]] = {
    "xml": {
        "one_tool_then_final": [
            "run_started",
            "context_built",
            "react_phase:reasoning",
            "model_request_start",
            "model_first_token?",
            "model_complete?",
            "react_phase:acting",
            "react_phase:observation",
            "tool_executed",
            "react_phase:recording",
            "context_built",
            "react_phase:reasoning",
            "model_request_start",
            "model_first_token?",
            "model_complete?",
            "react_phase:recording",
            "run_finished",
        ],
        "final_only": [
            "run_started",
            "context_built",
            "react_phase:reasoning",
            "model_request_start",
            "react_phase:recording",
            "run_finished",
        ],
    },
    "native": {
        "one_tool_then_final": [
            "run_started",
            "context_built",
            "model_request_start",
            "react_phase:reasoning",
            "react_phase:acting",
            "tool_executed",
            "react_phase:recording",
            "react_phase:observation",
            "react_phase:reasoning",
            "model_first_token?",
            "model_complete?",
            "react_phase:recording",
            "run_finished",
        ],
    },
}

_ORDER_RULES_COMMON: list[tuple[str, str]] = [
    ("context_built", "model_request_start"),
    ("react_phase:acting", "tool_executed"),
]

_ORDER_RULES_BY_PATH: dict[LoopPath, list[tuple[str, str]]] = {
    "xml": [
        ("tool_executed", "react_phase:recording"),
    ],
    "native": [
        ("tool_executed", "react_phase:recording"),
        ("react_phase:recording", "react_phase:observation"),
    ],
}


def normalize_event_name(event_row: dict) -> str:
    """将 trace 行规范为事件名（react_phase 展开为 react_phase:<phase>）。"""
    name = str(event_row.get("event", ""))
    if name != "react_phase":
        return name
    payload = event_row.get("payload") or {}
    phase = payload.get("phase", "")
    return f"react_phase:{phase}" if phase else name


def normalize_trace_events(events: list[dict]) -> list[str]:
    """批量规范化 trace 事件名列表。"""
    return [normalize_event_name(row) for row in events]


def _is_optional(pattern: str) -> bool:
    return pattern.endswith("?")


def _pattern_name(pattern: str) -> str:
    return pattern[:-1] if _is_optional(pattern) else pattern


def assert_subsequence(actual: list[str], expected: list[str]) -> None:
    """断言 *expected*（含可选 ? 项）按顺序出现在 *actual* 中。"""
    index = 0
    for pattern in expected:
        target = _pattern_name(pattern)
        optional = _is_optional(pattern)
        while index < len(actual) and actual[index] != target:
            index += 1
        if index >= len(actual):
            if optional:
                continue
            raise AssertionError(
                f"missing required event {target!r} in subsequence check; "
                f"actual={actual!r} expected={expected!r}"
            )
        index += 1


def validate_loop_trace(events: list[dict], *, path: LoopPath) -> list[str]:
    """校验 trace 是否满足 Agentic Loop 相对顺序约束；返回错误消息列表。"""
    names = normalize_trace_events(events)
    errors: list[str] = []

    if "run_started" not in names:
        errors.append("missing run_started")
    if "run_finished" not in names:
        errors.append("missing run_finished")
    if path == "xml" and "context_built" not in names:
        errors.append("xml path missing context_built")
    if path == "native" and "context_built" not in names:
        errors.append("native path missing context_built")

    for left, right in _ORDER_RULES_COMMON + _ORDER_RULES_BY_PATH.get(path, []):
        if left not in names or right not in names:
            continue
        if names.index(left) >= names.index(right):
            errors.append(f"{left} must precede {right}")

    tool_steps = names.count("tool_executed")
    if tool_steps:
        for phase in ("react_phase:acting", "react_phase:observation", "react_phase:recording"):
            if phase not in names:
                errors.append(f"tool step present but missing {phase}")

    return errors
