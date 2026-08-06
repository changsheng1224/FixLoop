"""验证结果分桶诊断：区分环境失败与逻辑失败，驱动下一轮反馈与停机。

能力向改进（非单例 case）：
- env：依赖缺失、Django settings、pip、空收集配置 → 继续改补丁通常无效
- collect：收集期错误但非明确环境 → 先修导入/路径，再谈断言
- logic：FAILED / AssertionError → 应读失败测试再改
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import StrEnum
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from src.state import VerificationResult

__all__ = [
    "VerifyBucket",
    "VerifyDiagnosis",
    "collect_log_excerpt",
    "diagnose_verification",
    "enrich_related_tests_from_diagnosis",
    "should_stop_on_env",
]

_NODE_ID_RE = re.compile(
    r"(?:^|\s)((?:[\w./\\-]+/)*[\w.-]+\.py(?:::[\w.\[\]]+)*)",
    re.MULTILINE,
)
_FAILED_LINE_RE = re.compile(
    r"(FAILED|ERROR)\s+([\w./\\-]+\.py(?:::[\w.\[\]]+)*)",
    re.IGNORECASE,
)


class VerifyBucket(StrEnum):
    ENV = "env"
    COLLECT = "collect"
    LOGIC = "logic"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class VerifyDiagnosis:
    bucket: VerifyBucket
    reason: str
    failed_nodeids: list[str] = field(default_factory=list)
    guidance: str = ""

    @property
    def is_env(self) -> bool:
        return self.bucket == VerifyBucket.ENV


_ENV_MARKERS = (
    "verify_config:",
    "未收集到任何测试",
    "sandbox pip install failed",
    "sandbox upload did not complete",
    "sandbox upload did not finish",
    "mkdir: unrecognized option",
    "ModuleNotFoundError",
    "ImportError:",
    "No module named",
    "ImproperlyConfigured",
    "DJANGO_SETTINGS_MODULE",
    "django.conf",
    "Settings are not configured",
    "ExecutableNotFound",
    "Could not find platform independent libraries",
)

_COLLECT_MARKERS = (
    "ERROR collecting",
    "collected 0 items",
    "no tests ran",
    "ImportPathMismatchError",
    "during collection",
)

_LOGIC_MARKERS = (
    "AssertionError",
    "assert ",
    " FAILED",
    "FAILED ",
    "E       assert",
    "E   assert",
)


def _joined_logs(result: VerificationResult) -> str:
    parts = list(result.failure_logs or [])
    if result.build_log:
        parts.append(result.build_log)
    return "\n".join(str(x) for x in parts)


def collect_log_excerpt(result: VerificationResult | None, *, max_chars: int = 600) -> str:
    """空收集/环境失败时截取可诊断原文（供 feedback，避免只见笼统标签）。"""
    if result is None:
        return ""
    chunks: list[str] = []
    for line in list(result.failure_logs or [])[:8]:
        s = str(line).strip()
        if s:
            chunks.append(s)
    if result.build_log:
        chunks.append(str(result.build_log).strip()[:400])
    text = "\n".join(chunks).strip()
    if len(text) > max_chars:
        return text[: max_chars - 20] + "\n...[truncated]..."
    return text


def _extract_nodeids(text: str) -> list[str]:
    found: list[str] = []
    seen: set[str] = set()
    for m in _FAILED_LINE_RE.finditer(text):
        nid = m.group(2).replace("\\", "/")
        if nid not in seen:
            seen.add(nid)
            found.append(nid)
    if found:
        return found[:8]
    for m in _NODE_ID_RE.finditer(text):
        nid = m.group(1).replace("\\", "/")
        if not nid.endswith(".py") and "::" not in nid:
            continue
        if nid not in seen:
            seen.add(nid)
            found.append(nid)
        if len(found) >= 8:
            break
    return found


def diagnose_verification(result: VerificationResult | None) -> VerifyDiagnosis:
    """从 VerificationResult 推断分桶与下一轮指导。"""
    if result is None:
        return VerifyDiagnosis(
            bucket=VerifyBucket.UNKNOWN,
            reason="no_result",
            guidance="验证结果缺失，请检查 verifier 配置。",
        )
    if result.all_passed:
        return VerifyDiagnosis(bucket=VerifyBucket.LOGIC, reason="passed", guidance="")

    text = _joined_logs(result)
    nodeids = _extract_nodeids(text)
    lower = text.lower()

    # 空收集 + 无断言 → 优先环境/配置
    empty_collect = result.total_tests == 0 and not result.all_passed

    if any(m in text for m in _ENV_MARKERS) or (
        empty_collect and ("pip" in lower or "docker" in lower or "sandbox" in lower)
    ):
        reason = "env_or_verify_config"
        for m in _ENV_MARKERS:
            if m in text:
                reason = m.rstrip(":")
                break
        excerpt = collect_log_excerpt(result)
        tip = (
            "验证环境/配置失败，继续改业务补丁通常无效。"
            "不要重复同一逻辑 diff；不要去「读失败测试改断言」。"
            "若必须重试，先确认 sandbox 可写、依赖与测试入口"
            "（settings / PYTHONPATH / 目标 nodeid）可用。"
        )
        if excerpt:
            tip = f"{tip}\n诊断摘录:\n{excerpt}"
        return VerifyDiagnosis(
            bucket=VerifyBucket.ENV,
            reason=reason,
            failed_nodeids=nodeids,
            guidance=tip,
        )

    # 断言/FAILED 优先于「total_tests==0」的收集桶（日志里常有 assert 但计数未解析）
    if any(m in text for m in _LOGIC_MARKERS) or result.failed > 0:
        tip = ""
        if nodeids:
            tip = (
                "先用 read_file 打开失败测试："
                + "；".join(nodeids[:3])
                + "。对照断言再改实现。"
            )
        else:
            tip = "对照失败断言修改实现；避免无关文件改动。"
        return VerifyDiagnosis(
            bucket=VerifyBucket.LOGIC,
            reason="test_assertion",
            failed_nodeids=nodeids,
            guidance=tip,
        )

    if empty_collect or any(m in text for m in _COLLECT_MARKERS):
        excerpt = collect_log_excerpt(result)
        tip = (
            "测试收集失败（total_tests=0）。优先检查被测模块导入路径、"
            "框架 settings、以及目标 nodeid 是否写对；"
            "先根据收集日志修入口，勿当作断言失败乱改业务逻辑。"
        )
        if excerpt:
            tip = f"{tip}\n收集日志摘录:\n{excerpt}"
        return VerifyDiagnosis(
            bucket=VerifyBucket.COLLECT,
            reason="collect_error",
            failed_nodeids=nodeids,
            guidance=tip,
        )

    return VerifyDiagnosis(
        bucket=VerifyBucket.UNKNOWN,
        reason="unclassified",
        failed_nodeids=nodeids,
        guidance="根据失败日志定位根因；环境错误勿盲目改业务逻辑。",
    )


def should_stop_on_env(*, consecutive_env: int, threshold: int = 2) -> bool:
    """连续环境失败达到阈值则停止烧 patch retry。"""
    return consecutive_env >= threshold


def enrich_related_tests_from_diagnosis(state, diagnosis: VerifyDiagnosis) -> None:
    """将失败 nodeid 注入 related_tests（置顶），供下一轮 patcher / verify 使用。"""
    from src.repair.verification.fail_surface import prioritize_failed_nodeids

    if not diagnosis.failed_nodeids:
        return
    prioritize_failed_nodeids(state, list(diagnosis.failed_nodeids))
