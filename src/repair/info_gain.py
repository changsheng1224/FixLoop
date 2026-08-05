"""信息增益：无新失败面/无新补丁文件则累计零增益，驱动长程换策略。

能力向（非单例）：避免 retries 烧在相同 env/相同 hunk 上。
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Iterable

if TYPE_CHECKING:
    from src.state import CandidatePatch, RepairState, VerificationResult

__all__ = [
    "InfoGainTracker",
    "apply_info_gain",
    "load_info_gain_from_state",
]


def _verify_face_hash(result: "VerificationResult | None") -> str:
    if result is None:
        return ""
    parts = list(result.failure_logs or [])[:6]
    if result.build_log:
        parts.append(str(result.build_log)[:400])
    blob = "\n".join(str(p)[:240] for p in parts)
    if not blob.strip():
        blob = f"t={result.total_tests}:f={result.failed}:p={int(bool(result.all_passed))}"
    return hashlib.sha256(blob.encode("utf-8", errors="replace")).hexdigest()[:16]


def _patch_files(patches: Iterable["CandidatePatch"] | None) -> frozenset[str]:
    out: set[str] = set()
    for p in patches or []:
        fp = str(getattr(p, "file_path", "") or "").replace("\\", "/")
        if fp:
            out.add(fp)
    return frozenset(out)


@dataclass
class InfoGainTracker:
    """跨 verify 回合追踪是否学到新信息。"""

    zero_gain_threshold: int = 2
    zero_gain_streak: int = 0
    last_verify_hash: str = ""
    last_patch_files: frozenset[str] = field(default_factory=frozenset)
    history: list[dict] = field(default_factory=list)

    def record(
        self,
        result: "VerificationResult | None",
        patches: Iterable["CandidatePatch"] | None,
    ) -> bool:
        """记录本轮；返回是否有信息增益。"""
        vhash = _verify_face_hash(result)
        files = _patch_files(patches)
        gained = False
        if vhash and vhash != self.last_verify_hash:
            gained = True
        if files and files != self.last_patch_files:
            gained = True
        if gained:
            self.zero_gain_streak = 0
        else:
            self.zero_gain_streak += 1
        self.last_verify_hash = vhash or self.last_verify_hash
        if files:
            self.last_patch_files = files
        self.history.append(
            {
                "gained": gained,
                "verify_hash": vhash,
                "files": sorted(files),
                "zero_gain_streak": self.zero_gain_streak,
            }
        )
        if len(self.history) > 12:
            self.history = self.history[-12:]
        return gained

    def should_force_shift(self) -> bool:
        return self.zero_gain_streak >= self.zero_gain_threshold

    def to_dict(self) -> dict:
        return {
            "zero_gain_threshold": self.zero_gain_threshold,
            "zero_gain_streak": self.zero_gain_streak,
            "last_verify_hash": self.last_verify_hash,
            "last_patch_files": sorted(self.last_patch_files),
            "history": list(self.history[-6:]),
        }

    @classmethod
    def from_dict(cls, raw: dict | None) -> "InfoGainTracker":
        if not isinstance(raw, dict):
            return cls()
        files = raw.get("last_patch_files") or []
        return cls(
            zero_gain_threshold=int(raw.get("zero_gain_threshold") or 2),
            zero_gain_streak=int(raw.get("zero_gain_streak") or 0),
            last_verify_hash=str(raw.get("last_verify_hash") or ""),
            last_patch_files=frozenset(str(x) for x in files),
            history=list(raw.get("history") or [])[-6:],
        )


def apply_info_gain(state: "RepairState", tracker: InfoGainTracker) -> None:
    state.node_timings["info_gain"] = tracker.to_dict()


def load_info_gain_from_state(state: "RepairState") -> InfoGainTracker:
    return InfoGainTracker.from_dict(state.node_timings.get("info_gain"))
