"""VerifyCooldown 冷却轮单测（V1.4-Bonus15e）。"""

from __future__ import annotations

from src.repair.verification.verify_cooldown import VerifyCooldown, _hash_failures


class TestHashFailures:
    def test_empty(self):
        assert _hash_failures([]) == ""

    def test_same_logs_same_hash(self):
        logs = ["test_a FAILED", "test_b FAILED"]
        assert _hash_failures(logs) == _hash_failures(logs)

    def test_different_logs_different_hash(self):
        assert _hash_failures(["test_a FAILED"]) != _hash_failures(["test_b FAILED"])


class TestVerifyCooldown:
    def test_first_failure_no_cooldown(self):
        vc = VerifyCooldown()
        triggered = vc.record_failure(["test_x FAILED"])
        assert not triggered
        assert not vc.cooldown_active

    def test_two_different_failures_no_cooldown(self):
        vc = VerifyCooldown()
        vc.record_failure(["test_a FAILED"])
        triggered = vc.record_failure(["test_b FAILED"])
        assert not triggered
        assert not vc.cooldown_active

    def test_two_consecutive_same_triggers_cooldown(self):
        vc = VerifyCooldown()
        vc.record_failure(["test_x FAILED"])
        triggered = vc.record_failure(["test_x FAILED"])
        assert triggered
        assert vc.cooldown_active
        assert vc.suggested_temperature == 0.5
        assert "连续" in vc.cooldown_hint

    def test_success_resets_cooldown(self):
        vc = VerifyCooldown()
        vc.record_failure(["test_x FAILED"])
        vc.record_failure(["test_x FAILED"])
        assert vc.cooldown_active
        vc.record_success()
        assert not vc.cooldown_active
        # 再次失败从头计数
        assert not vc.record_failure(["test_x FAILED"])

    def test_different_failure_resets_counter(self):
        vc = VerifyCooldown()
        vc.record_failure(["test_x FAILED"])
        vc.record_failure(["test_y FAILED"])  # different
        # 再相同 → count=2 but not consecutive with same hash
        assert not vc.record_failure(["test_x FAILED"])
        vc.record_failure(["test_x FAILED"])
        assert vc.cooldown_active

    def test_cooldown_hint_when_inactive(self):
        vc = VerifyCooldown()
        assert vc.cooldown_hint == ""
