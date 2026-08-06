"""Provider-neutral response envelope for structured runtime outputs."""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass, field
from typing import Any


@dataclass
class CanonicalResponse:
    response_id: str
    response_kind: str
    status: str
    payload: dict[str, Any] = field(default_factory=dict)
    raw_ref: str = ""
    parse_errors: list[dict[str, Any]] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @classmethod
    def create(
        cls,
        response_kind: str,
        status: str,
        payload: dict[str, Any] | None = None,
        **kwargs,
    ) -> CanonicalResponse:
        body = payload or {}
        stable = json.dumps(body, sort_keys=True, ensure_ascii=True, default=str)
        rid = "RESP-" + hashlib.sha256(
            f"{response_kind}:{status}:{stable}".encode()
        ).hexdigest()[:12]
        return cls(rid, response_kind, status, body, **kwargs)

    def to_dict(self) -> dict[str, Any]:
        return _json_safe(asdict(self))


def _json_safe(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, list | tuple):
        return [_json_safe(item) for item in value]
    if hasattr(value, "to_dict") and callable(value.to_dict):
        return _json_safe(value.to_dict())
    return value
