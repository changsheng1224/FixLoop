"""Temporary runtime controls for a Patcher turn."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class PatcherDepthToken:
    previous_max_steps: int


def push_patcher_depth(agent, max_steps: int) -> PatcherDepthToken | None:
    """Temporarily override the Patcher turn limit and return a restore token."""
    config = getattr(agent, "config", None) if agent is not None else None
    if config is None or not hasattr(config, "max_steps") or max_steps <= 0:
        return None
    token = PatcherDepthToken(previous_max_steps=int(config.max_steps or 0))
    config.max_steps = int(max_steps)
    return token


def pop_patcher_depth(agent, token: PatcherDepthToken | None) -> None:
    """Restore a Patcher turn limit previously overridden by the runtime."""
    config = getattr(agent, "config", None) if agent is not None else None
    if config is not None and hasattr(config, "max_steps") and token is not None:
        config.max_steps = token.previous_max_steps
