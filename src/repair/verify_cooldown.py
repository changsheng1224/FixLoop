"""Verify 失败冷却轮（V1.4-Bonus15e）。

连续相同 verify 失败 → 降 temperature + 建议换策略，防震荡。
"""

from __future__ import annotations

import hashlib

_COOLDOWN_MAX_CONSECUTIVE = 2  # 连续 N 次相同失败触发冷却
_TEMPERATURE_REDUCTION = 0.5   # 冷却时 temperature 降至此值


class VerifyCooldown:
    """追踪连续 verify 失败，触发冷却策略。"""

    def __init__(self):
        self._last_hash = ""
        self._consecutive = 0
        self.cooldown_active = False
        self.suggested_temperature: float | None = None

    def record_failure(self, failure_logs: list[str]) -> bool:
        """记录一次 verify 失败。

        Args:
            failure_logs: 失败日志列表。

        Returns:
            True 表示冷却已激活（连续相同失败），False 表示正常。
        """
        current_hash = _hash_failures(failure_logs)
        if current_hash == self._last_hash and self._last_hash:
            self._consecutive += 1
        else:
            self._consecutive = 1
            self._last_hash = current_hash
            self.cooldown_active = False
            self.suggested_temperature = None

        if self._consecutive >= _COOLDOWN_MAX_CONSECUTIVE:
            self.cooldown_active = True
            self.suggested_temperature = _TEMPERATURE_REDUCTION
            return True
        return False

    def record_success(self) -> None:
        """verify 成功 → 重置。"""
        self._last_hash = ""
        self._consecutive = 0
        self.cooldown_active = False
        self.suggested_temperature = None

    @property
    def cooldown_hint(self) -> str:
        if self.cooldown_active:
            return (
                f"⚠ 连续 {self._consecutive} 次相同验证失败。"
                "请尝试不同的修复策略，避免重复相同补丁。"
            )
        return ""


def _hash_failures(logs: list[str]) -> str:
    if not logs:
        return ""
    combined = "\n".join(str(l)[:200] for l in logs[:5])
    return hashlib.sha256(combined.encode()).hexdigest()[:16]
