"""长程止损：有进展才继续烧 retry，无进展 / 震荡则早停。

能力向（非单例）：
- identical_patch：同一补丁指纹连续出现
- identical_verify：同一验证失败哈希连续出现（冷却之后仍不变）
- apply_thrash / parse_thrash：连续空补丁
- no_progress：连续回合既无新补丁也无验证结果变化
- env：连续环境失败（与 verify_diagnose 对齐）

长程含义：只要本回合有「新补丁」或「验证失败面变化」，不触发无进展止损，
允许在 max_retries 内继续探索。
"""

from __future__ import annotations

import hashlib
from collections.abc import Iterable
from dataclasses import dataclass, field
from enum import StrEnum
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from src.state import CandidatePatch, RepairState, VerificationResult

__all__ = [
    "StopLossDecision",
    "StopLossReason",
    "StopLossTracker",
    "apply_stop_loss",
    "has_stop_loss",
    "patch_fingerprint",
]


class StopLossReason(StrEnum):
    IDENTICAL_PATCH = "identical_patch"
    IDENTICAL_VERIFY = "identical_verify"
    APPLY_THRASH = "apply_thrash"
    PARSE_THRASH = "parse_thrash"
    NO_PROGRESS = "no_progress"
    ENV = "env"


@dataclass(frozen=True)
class StopLossDecision:
    stop: bool
    reason: str = ""
    progress: bool = False
    hint: str = ""
    meta: dict[str, object] = field(default_factory=dict)


def patch_fingerprint(patch: CandidatePatch) -> str:
    def _as_text(value) -> str:
        if value is None:
            return ""
        if isinstance(value, list):
            return "\n".join(str(x) for x in value)
        return str(value)

    body = "\n".join(
        [
            _as_text(patch.file_path),
            _as_text(patch.diff),
            _as_text(patch.original_lines),
            _as_text(patch.patched_lines),
        ]
    )
    return hashlib.sha256(body.encode("utf-8")).hexdigest()[:16]


def _no_progress_meta(streak: int) -> dict[str, object]:
    required = "write_patch_or_expand_context"
    allowed = ["write_patch", "expand_context", "replan"]
    if streak >= 3:
        required = "stop_repeated_reads_and_replan"
        allowed = ["write_patch", "replan", "terminate"]
    return {
        "no_progress_count": streak,
        "required_next_action": required,
        "allowed_next_actions": allowed,
        "forbid_repeated_reads": streak >= 2,
        "control_signal": "soft_warning" if streak == 2 else "hard_stop",
    }


def _hash_verify_logs(logs: Iterable[str] | None) -> str:
    if not logs:
        return ""
    combined = "\n".join(str(line)[:200] for line in list(logs)[:5])
    return hashlib.sha256(combined.encode()).hexdigest()[:16]


@dataclass
class StopLossTracker:
    """跨 patch/verify 回合追踪进展与止损条件。"""

    identical_patch_threshold: int = 2
    identical_verify_threshold: int = 3
    apply_thrash_threshold: int = 2
    parse_thrash_threshold: int = 2
    no_progress_threshold: int = 2
    env_threshold: int = 2

    _seen_fps: set[str] = field(default_factory=set)
    _last_fps: tuple[str, ...] = ()
    _last_verify_hash: str = ""
    _identical_patch_streak: int = 0
    _identical_verify_streak: int = 0
    _empty_streak: int = 0
    _apply_fail_streak: int = 0
    _parse_fail_streak: int = 0
    _no_progress_streak: int = 0
    _env_streak: int = 0
    _attempts: int = 0

    def record_empty_patch(self, *, apply_failed: bool) -> StopLossDecision:
        """无候选补丁（parse 失败或 apply 失败）。"""
        self._attempts += 1
        self._empty_streak += 1
        self._no_progress_streak += 1
        self._identical_patch_streak = 0
        self._last_fps = ()
        if apply_failed:
            self._apply_fail_streak += 1
            self._parse_fail_streak = 0
        else:
            self._parse_fail_streak += 1
            self._apply_fail_streak = 0

        if self._apply_fail_streak >= self.apply_thrash_threshold:
            return StopLossDecision(
                stop=True,
                reason=StopLossReason.APPLY_THRASH,
                hint=(
                    f"连续 {self._apply_fail_streak} 次补丁无法落盘。"
                    "停止重复同一 apply 路径；请改用 grounded read + patch_file。"
                ),
            )
        if self._parse_fail_streak >= self.parse_thrash_threshold:
            return StopLossDecision(
                stop=True,
                reason=StopLossReason.PARSE_THRASH,
                hint=(
                    f"连续 {self._parse_fail_streak} 次未产出可解析补丁。"
                    "停止空转；需先探索再编辑。"
                ),
                meta=_no_progress_meta(self._no_progress_streak),
            )
        if self._no_progress_streak >= self.no_progress_threshold:
            return StopLossDecision(
                stop=True,
                reason=StopLossReason.NO_PROGRESS,
                hint="连续多回合无有效补丁进展，触发止损。",
                meta=_no_progress_meta(self._no_progress_streak),
            )
        meta = (
            _no_progress_meta(self._no_progress_streak)
            if self._no_progress_streak >= self.no_progress_threshold
            else {}
        )
        return StopLossDecision(stop=False, progress=False, meta=meta)

    def record_verify_failure(
        self,
        result: VerificationResult,
        patches: list[CandidatePatch],
    ) -> StopLossDecision:
        """有补丁但验证失败：按指纹/失败面判断是否仍有进展。"""
        from src.repair.verification.verify_diagnose import VerifyBucket, diagnose_verification

        self._attempts += 1
        self._empty_streak = 0
        self._apply_fail_streak = 0
        self._parse_fail_streak = 0

        fps = tuple(sorted({patch_fingerprint(p) for p in patches if p}))
        verify_hash = _hash_verify_logs(result.failure_logs if result else None)
        diag = diagnose_verification(result)

        novel = any(fp not in self._seen_fps for fp in fps)
        for fp in fps:
            self._seen_fps.add(fp)

        if fps and fps == self._last_fps:
            self._identical_patch_streak += 1
        else:
            self._identical_patch_streak = 1 if fps else 0
        self._last_fps = fps

        prev_hash = self._last_verify_hash
        if verify_hash and verify_hash == prev_hash:
            self._identical_verify_streak += 1
        else:
            self._identical_verify_streak = 1 if verify_hash else 0
        verify_changed = bool(verify_hash) and verify_hash != prev_hash
        self._last_verify_hash = verify_hash

        if diag.bucket == VerifyBucket.ENV:
            self._env_streak += 1
        else:
            self._env_streak = 0

        # 进展：新补丁，或（非 env 下）失败面变化
        progress = novel or (verify_changed and diag.bucket != VerifyBucket.ENV)
        if self._attempts <= 1:
            self._no_progress_streak = 0
            progress = True
        elif progress:
            self._no_progress_streak = 0
        else:
            self._no_progress_streak += 1

        if self._env_streak >= self.env_threshold:
            return StopLossDecision(
                stop=True,
                reason=StopLossReason.ENV,
                progress=False,
                hint=(
                    f"连续 {self._env_streak} 次验证环境失败（{diag.reason}），"
                    "停止继续改业务补丁。"
                ),
            )
        if self._no_progress_streak >= 3:
            return StopLossDecision(
                stop=True,
                reason=StopLossReason.NO_PROGRESS,
                progress=False,
                hint=(
                    f"连续 {self._no_progress_streak} 回合无新补丁且验证失败面不变。"
                    "停止重复读取；请直接写补丁、换假设或扩展检索。"
                ),
                meta=_no_progress_meta(self._no_progress_streak),
            )
        if self._identical_patch_streak >= self.identical_patch_threshold:
            return StopLossDecision(
                stop=True,
                reason=StopLossReason.IDENTICAL_PATCH,
                progress=False,
                hint=(
                    f"连续 {self._identical_patch_streak} 次产出相同补丁指纹。"
                    "停止重复 diff；换文件、换断言相关表达式或先 read_file 再改。"
                ),
            )
        if self._identical_verify_streak >= self.identical_verify_threshold:
            return StopLossDecision(
                stop=True,
                reason=StopLossReason.IDENTICAL_VERIFY,
                progress=False,
                hint=(
                    f"连续 {self._identical_verify_streak} 次相同验证失败。"
                    "冷却已无效，触发止损以免空烧预算。"
                ),
            )
        if self._no_progress_streak >= self.no_progress_threshold:
            return StopLossDecision(
                stop=False,
                reason=StopLossReason.NO_PROGRESS,
                progress=False,
                hint=(
                    f"连续 {self._no_progress_streak} 回合无新补丁且验证失败面不变。"
                    "请停止重复读取，改为写补丁或切换假设。"
                ),
                meta=_no_progress_meta(self._no_progress_streak),
            )
        return StopLossDecision(stop=False, progress=progress)

    def snapshot(self) -> dict:
        return {
            "attempts": self._attempts,
            "identical_patch_streak": self._identical_patch_streak,
            "identical_verify_streak": self._identical_verify_streak,
            "no_progress_streak": self._no_progress_streak,
            "env_streak": self._env_streak,
            "apply_fail_streak": self._apply_fail_streak,
            "parse_fail_streak": self._parse_fail_streak,
            "seen_fps": len(self._seen_fps),
        }


def apply_stop_loss(state: RepairState, decision: StopLossDecision) -> None:
    """把止损写入 state，供终态 / degrade / tags 使用。"""
    if decision.reason == StopLossReason.NO_PROGRESS and decision.meta:
        state.node_timings["no_progress_warning"] = dict(decision.meta)
        state.node_timings["no_progress_control"] = {
            "required_next_action": decision.meta.get("required_next_action", ""),
            "forbid_repeated_reads": bool(decision.meta.get("forbid_repeated_reads")),
            "allowed_next_actions": list(decision.meta.get("allowed_next_actions") or []),
        }
    if not decision.stop:
        return
    state.node_timings["stop_loss"] = decision.reason
    state.node_timings["stop_loss_early"] = True
    if decision.meta:
        state.node_timings["stop_loss_meta"] = dict(decision.meta)
        if "no_progress_count" in decision.meta:
            state.node_timings["no_progress_count"] = decision.meta["no_progress_count"]
    state.agent_errors["stop_loss"] = decision.hint or decision.reason
    extra = f"[止损]\n{decision.hint or decision.reason}"
    state.feedback = f"{state.feedback}\n\n{extra}".strip() if state.feedback else extra


def has_stop_loss(state: RepairState) -> bool:
    return bool(state.node_timings.get("stop_loss") or state.node_timings.get("stop_loss_early"))
