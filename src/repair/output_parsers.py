"""Agent 输出 JSON 解析（Localizer / Retriever / Verifier）。"""

from __future__ import annotations

import json
import logging
import re
from typing import Any

from src.repair.patch_applier import extract_json_block
from src.state import RetrievedContext, SuspectLocation, VerificationResult


def _repair_trailing_comma(text: str) -> str:
    """轻量修复 trailing comma: 移除 },] 前的多余逗号。"""
    # },] → }]
    text = re.sub(r",\s*(\}|\])", r"\1", text)
    # ,\n] → \n]
    text = re.sub(r",(\s*\])", r"\1", text)
    return text


def _strip_json_comments(text: str) -> str:
    """移除 // 和 /* */ 注释（轻量实现，不处理字符串内注释）。"""
    # 移除 // 行注释
    text = re.sub(r"//[^\n]*", "", text)
    # 移除 /* */ 块注释
    text = re.sub(r"/\*.*?\*/", "", text, flags=re.DOTALL)
    return text


def _load_json(text: str) -> Any | None:
    """四级降级解析：strict JSON → repair trailing comma → regex extract → None。"""
    # L1: strict JSON（含 markdown code block 提取）
    try:
        return json.loads(extract_json_block(text))
    except Exception:
        pass

    # L1.5: 修复 trailing comma + 注释 + retry
    try:
        repaired = _repair_trailing_comma(text)
        repaired = _strip_json_comments(repaired)
        return json.loads(extract_json_block(repaired))
    except Exception:
        pass

    # L2: regex 提取第一个 {...} 块 + 修复
    try:
        match = re.search(r"\{(?:[^{}]|\{[^{}]*\})*\}", text, re.DOTALL)
        if match:
            raw = match.group()
            raw = _repair_trailing_comma(raw)
            return json.loads(raw)
    except Exception:
        pass

    # L3: 空结构
    return None


def parse_suspect_list(answer: str) -> list[SuspectLocation]:
    data = _load_json(answer)
    errors: list[str] = []
    if not isinstance(data, list):
        errors.append("输出不是 JSON 数组")
        return _with_validation_errors([], errors)
    result = []
    for i, item in enumerate(data):
        if not isinstance(item, dict):
            errors.append(f"[{i}] 不是对象")
            continue
        if not item.get("file_path"):
            errors.append(f"[{i}] 缺少必填字段 file_path")
            continue
        try:
            result.append(SuspectLocation.from_dict(item))
        except Exception as e:
            errors.append(f"[{i}] {e}")
    return _with_validation_errors(result, errors)


_log = logging.getLogger("fixloop.output_parsers")


def _with_validation_errors(result, errors: list[str]):
    """记录 schema 校验错误（供 feedback 重试）。"""
    if errors:
        _log.warning("schema 校验: %s", "; ".join(errors[:3]))
    return result


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
    errors: list[str] = []
    if not isinstance(data, dict):
        errors.append("输出不是 JSON 对象")
        return _with_validation_errors(RetrievedContext(), errors)
    if "related_tests" in data:
        data["related_tests"] = _normalize_related_tests(data["related_tests"])
    if not data.get("related_tests"):
        errors.append("缺少 related_tests 字段")
    try:
        return _with_validation_errors(RetrievedContext.from_dict(data), errors)
    except Exception as e:
        errors.append(str(e))
        return _with_validation_errors(RetrievedContext(), errors)


def parse_verification(answer: str) -> VerificationResult:
    data = _load_json(answer)
    errors: list[str] = []
    if not isinstance(data, dict):
        errors.append("输出不是 JSON 对象")
        return _with_validation_errors(VerificationResult(), errors)
    try:
        return _with_validation_errors(VerificationResult.from_dict(data), errors)
    except Exception as e:
        errors.append(str(e))
        return _with_validation_errors(VerificationResult(), errors)
