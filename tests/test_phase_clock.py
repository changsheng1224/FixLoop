"""RepairPhaseClock 单测。"""

from __future__ import annotations

import time

import pytest

from src.repair.phase_clock import PhaseTimeoutConfig, PhaseTimeoutError, RepairPhaseClock


class TestRepairPhaseClock:
    def test_ensure_passes_with_budget(self):
        clock = RepairPhaseClock(PhaseTimeoutConfig(localize_s=10))
        clock.ensure("localize")

    def test_consume_raises_when_exceeds_localize_budget(self):
        clock = RepairPhaseClock(PhaseTimeoutConfig(localize_s=1))
        with pytest.raises(PhaseTimeoutError) as exc:
            clock.consume("localize", 1500)
        assert exc.value.phase == "localize"
        assert exc.value.budget_s == 1

    def test_patch_budget_is_cumulative(self):
        clock = RepairPhaseClock(PhaseTimeoutConfig(patch_s=2))
        clock.consume("patch", 1000)
        clock.ensure("patch")
        clock.consume("patch", 1000)
        with pytest.raises(PhaseTimeoutError) as exc:
            clock.ensure("patch")
        assert exc.value.phase == "patch"

    def test_repair_total_hard_cap(self):
        clock = RepairPhaseClock(
            PhaseTimeoutConfig(localize_s=60, patch_s=60, verify_s=60, repair_total_s=1)
        )
        time.sleep(1.05)
        with pytest.raises(PhaseTimeoutError) as exc:
            clock.ensure("localize")
        assert exc.value.phase == "repair_total"

    def test_zero_budget_disables_phase(self):
        clock = RepairPhaseClock(PhaseTimeoutConfig(localize_s=0))
        clock.consume("localize", 999_000)

    def test_from_repair_timeout_zero_disables_all(self):
        cfg = PhaseTimeoutConfig.from_repair_timeout(0)
        assert cfg.localize_s == 0
        assert cfg.patch_s == 0
        assert cfg.verify_s == 0
        assert cfg.repair_total_s == 0
        assert not cfg.any_enabled()

    def test_from_repair_timeout_positive_sets_total(self):
        cfg = PhaseTimeoutConfig.from_repair_timeout(300)
        assert cfg.repair_total_s == 300
        assert cfg.localize_s == 60
        assert cfg.any_enabled()
