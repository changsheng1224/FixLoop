"""Agent 输出 JSON 解析（Localizer / Retriever / Verifier）。"""

from __future__ import annotations

import json
from typing import Any

from src.repair.patch_applier import extract_json_block
from src.state import RetrievedContext, SuspectLocation, VerificationResult


def _load_json(text: str) -> Any | None:
    try:
        return json.loads(extract_json_block(text))
    except (json.JSONDecodeError, KeyError, TypeError):
        return None


def parse_suspect_list(answer: str) -> list[SuspectLocation]:
    data = _load_json(answer)
    if isinstance(data, list):
        return [SuspectLocation.from_dict(item) for item in data]
    return []


def _normalize_related_tests(raw: list) -> list[str]:
    normalized: list[str] = []
    for item in raw:
        if isinstance(item, str):
            normalized.append(item)
        elif isinstance(item, dict):
            normalized.append(item.get("name", item.get("path", json.dumps(item))))
        else:
            normalized.append(str(item))
    return normalized


def parse_retrieved_context(answer: str) -> RetrievedContext:
    data = _load_json(answer)
    if isinstance(data, dict):
        if "related_tests" in data:
            data["related_tests"] = _normalize_related_tests(data["related_tests"])
        return RetrievedContext.from_dict(data)
    return RetrievedContext()


def parse_verification(answer: str) -> VerificationResult:
    data = _load_json(answer)
    if isinstance(data, dict):
        return VerificationResult.from_dict(data)
    return VerificationResult()
