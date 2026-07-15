"""Structured log correlation fields (run_id, agent) via contextvars."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar, Token

_run_id: ContextVar[str | None] = ContextVar("fixloop_log_run_id", default=None)
_agent: ContextVar[str | None] = ContextVar("fixloop_log_agent", default=None)


def get_log_context() -> dict[str, str]:
    """Return non-empty correlation fields for JSON log records."""
    ctx: dict[str, str] = {}
    run_id = _run_id.get()
    if run_id:
        ctx["run_id"] = run_id
    agent = _agent.get()
    if agent:
        ctx["agent"] = agent
    return ctx


def bind_run_id(run_id: str | None) -> Token:
    """Bind run_id for the current context; return token for reset."""
    return _run_id.set(run_id)


def reset_run_id(token: Token) -> None:
    """Restore run_id from a prior bind_run_id token."""
    _run_id.reset(token)


@contextmanager
def log_context(
    *,
    run_id: str | None = None,
    agent: str | None = None,
) -> Iterator[None]:
    """Temporarily set log correlation fields (nested-safe)."""
    tokens: list[tuple[str, Token]] = []
    try:
        if run_id is not None:
            tokens.append(("run_id", _run_id.set(run_id)))
        if agent is not None:
            tokens.append(("agent", _agent.set(agent)))
        yield
    finally:
        for field, token in reversed(tokens):
            if field == "run_id":
                _run_id.reset(token)
            else:
                _agent.reset(token)
