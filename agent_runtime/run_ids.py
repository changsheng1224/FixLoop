"""Unified run_id generation for Layer 1 and Layer 2."""

from __future__ import annotations

import uuid


def new_run_id() -> str:
    """Return a new UUID v4 string for ``.agent/runs/{run_id}/``."""
    return str(uuid.uuid4())


def is_valid_run_id(value: str) -> bool:
    """Return True if *value* is a canonical UUID string."""
    if not value:
        return False
    try:
        uuid.UUID(value)
        return True
    except ValueError:
        return False
