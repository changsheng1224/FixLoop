"""ReAct 四阶段 trace 语义（reasoning → acting → observation → recording）。"""

from __future__ import annotations

from enum import StrEnum
from typing import Literal

ReactPath = Literal["xml", "native"]
LoopPath = ReactPath

__all__ = [
    "LoopPath",
    "ReactPhase",
    "ReactPath",
    "build_react_phase_payload",
]


class ReactPhase(StrEnum):
    REASONING = "reasoning"
    ACTING = "acting"
    OBSERVATION = "observation"
    RECORDING = "recording"


def build_react_phase_payload(
    phase: ReactPhase | str,
    *,
    step: int,
    path: ReactPath,
    tool: str | None = None,
) -> dict:
    """构造 react_phase trace 事件 payload。"""
    payload: dict = {
        "phase": str(phase),
        "step": int(step),
        "path": path,
    }
    if tool:
        payload["tool"] = tool
    return payload
