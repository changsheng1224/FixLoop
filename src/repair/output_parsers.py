"""Agent 输出 JSON 解析（Localizer / Retriever / Verifier）。"""

from __future__ import annotations

import json
import re
from typing import Any

from src.repair.patch_applier import extract_json_block
from src.state import RetrievedContext, SuspectLocation, VerificationResult


def _load_json(text: str) -> Any | None:
    """三级降级解析：strict JSON → regex extract → None。"""
    # L1: strict JSON（含 markdown code block 提取）
    try:
        return json.loads(extract_json_block(text))
    except Exception:
        pass
    # L2: regex 提取第一个 {...} 块
    try:
        match = re.search(r"\{.*\}", text, re.DOTALL)
        if match:
            return json.loads(match.group())
    except Exception:
        pass
    # L3: 空结构
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
