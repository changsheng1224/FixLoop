"""嫌疑置信分层与 patch 门禁。"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from src.state import SuspectLocation

__all__ = [
    "PatchGateDecision",
    "SuspectTier",
    "decide_patch_gate",
    "tier_for_suspect",
]

# HIGH: 强锚；MID: 可进 patch 但宜短修；LOW: 仅辅助，不足以单独开门
_HIGH_REASONS = frozenset(
    {
        "堆栈指向",
        "F2P覆盖",
        "test_patch覆盖",
        "issue 路径",
        "localize_confirmed",
    }
)
_MID_REASONS = frozenset(
    {
        "grep命中",
        "测试覆盖边",
        "语义扩展",
        "issue 符号",
        "测试导入",
        "测试导入模块",
    }
)


class SuspectTier:
    HIGH = "high"
    MID = "mid"
    LOW = "low"
    SKIP = "skip"  # 测试文件或不存在


def tier_for_suspect(suspect: "SuspectLocation", repo_root: str | Path) -> str:
    from src.repair.localize_quality import _is_test_path, normalize_repo_path

    root = Path(repo_root)
    rel = normalize_repo_path(getattr(suspect, "file_path", "") or "", root)
    if not rel or not (root / rel).is_file():
        return SuspectTier.SKIP
    if _is_test_path(rel):
        return SuspectTier.LOW
    reason = (getattr(suspect, "reason", "") or "").strip()
    if reason in _HIGH_REASONS or reason.startswith("堆栈"):
        return SuspectTier.HIGH
    if reason in _MID_REASONS or reason.startswith("F2P"):
        # F2P测试 → low；F2P覆盖 already high
        if reason == "F2P测试":
            return SuspectTier.LOW
        if reason.startswith("F2P") and "覆盖" in reason:
            return SuspectTier.HIGH
        return SuspectTier.MID
    conf = float(getattr(suspect, "confidence", 0) or 0)
    if conf >= 0.85:
        return SuspectTier.HIGH
    if conf >= 0.55:
        return SuspectTier.MID
    return SuspectTier.LOW


@dataclass(frozen=True)
class PatchGateDecision:
    allow: bool
    force_short_repair: bool
    reason: str
    high_count: int = 0
    mid_count: int = 0
    low_count: int = 0

    def to_dict(self) -> dict:
        return {
            "allow": self.allow,
            "force_short_repair": self.force_short_repair,
            "reason": self.reason,
            "high_count": self.high_count,
            "mid_count": self.mid_count,
            "low_count": self.low_count,
        }


def decide_patch_gate(
    suspects: list["SuspectLocation"] | None,
    repo_root: str | Path,
) -> PatchGateDecision:
    """分层门禁：HIGH 直接放行；仅 MID/LOW(实现) 放行但强制短修；无可编辑实现 → 拦截。"""
    from src.repair.localize_quality import _is_test_path, normalize_repo_path

    root = Path(repo_root)
    high = mid = low = 0
    for s in suspects or []:
        t = tier_for_suspect(s, repo_root)
        if t == SuspectTier.HIGH:
            high += 1
        elif t == SuspectTier.MID:
            mid += 1
        elif t == SuspectTier.LOW:
            rel = normalize_repo_path(getattr(s, "file_path", "") or "", root)
            # 测试文件的 LOW 不算可编辑实现锚
            if rel and not _is_test_path(rel):
                low += 1
    if high > 0:
        return PatchGateDecision(
            allow=True,
            force_short_repair=False,
            reason="high_tier_impl",
            high_count=high,
            mid_count=mid,
            low_count=low,
        )
    if mid > 0:
        return PatchGateDecision(
            allow=True,
            force_short_repair=True,
            reason="mid_tier_short_repair",
            high_count=high,
            mid_count=mid,
            low_count=low,
        )
    if low > 0:
        # P0：不再因仅 LOW 硬拦 patch（matplotlib 类空锚早退）
        return PatchGateDecision(
            allow=True,
            force_short_repair=True,
            reason="low_tier_short_repair",
            high_count=high,
            mid_count=mid,
            low_count=low,
        )
    return PatchGateDecision(
        allow=False,
        force_short_repair=False,
        reason="no_editable_impl",
        high_count=high,
        mid_count=mid,
        low_count=low,
    )
