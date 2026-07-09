"""timing_schema 单测。"""

from src.repair.timing_schema import (
    finalize_phases,
    get_phase_ms,
    phases_for_report,
    set_parallel_wall_ms,
    set_phase_ms,
    set_repair_total_ms,
)


class TestTimingSchema:
    def test_set_phase_ms_writes_canonical_and_legacy(self):
        timings: dict = {}
        set_phase_ms(
            timings,
            "localize",
            1200,
            internal={"model_call_ms": 1100, "tool_exec_ms": 100},
        )
        assert timings["phases"]["localize_ms"] == 1200
        assert timings["localizer_ms"] == 1200
        assert timings["phases_internal"]["localize"]["model_call_ms"] == 1100
        assert timings["localizer_internal"]["tool_exec_ms"] == 100

    def test_set_repair_total_ms(self):
        timings: dict = {}
        set_repair_total_ms(timings, 5500)
        assert timings["phases"]["repair_total_ms"] == 5500

    def test_parallel_wall_ms(self):
        timings: dict = {}
        set_parallel_wall_ms(timings, 900)
        assert timings["parallel_wall_ms"]["localize_retrieve_ms"] == 900
        assert timings["localize_retrieve_ms"] == 900

    def test_finalize_syncs_legacy_to_phases(self):
        timings = {"localizer_ms": 500, "patcher_ms": 200}
        finalize_phases(timings)
        assert timings["phases"]["localize_ms"] == 500
        assert timings["phases"]["patch_ms"] == 200

    def test_get_phase_ms_prefers_canonical(self):
        timings = {"phases": {"patch_ms": 300}, "patcher_ms": 999}
        assert get_phase_ms(timings, "patch") == 300

    def test_phases_for_report(self):
        timings: dict = {}
        set_phase_ms(timings, "verify", 42)
        set_repair_total_ms(timings, 100)
        report_phases = phases_for_report(timings)
        assert report_phases["verify_ms"] == 42
        assert report_phases["repair_total_ms"] == 100
