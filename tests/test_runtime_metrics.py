"""runtime_metrics 统一字段单测。"""

from src.repair.run_trace import RepairRunTracer


class TestRuntimeMetricsReport:
    def test_l2_report_includes_runtime_metrics(self, tmp_path):
        """L2 report 含 runtime_metrics 字段。"""

        from src.state import RepairPlan, RepairState

        state = RepairState(issue_input="test")
        state.repair_run_id = "r-001"
        state.retry_count = 2
        state.repair_plan = RepairPlan(issue_type="type_error")

        tracer = RepairRunTracer(str(tmp_path))
        tracer.run_id = "r-001"
        tracer.store.start_run_by_id("r-001")

        # 模拟 finalize 写 report
        token_summary = {"total_tokens": 1000, "cache_hit_rate": 0.75}
        tracer.finalize(state, token_summary)

        import json

        report_path = tracer.store.runs_dir / "r-001" / "report.json"
        report = json.loads(report_path.read_text(encoding="utf-8"))

        assert "runtime_metrics" in report
        rm = report["runtime_metrics"]
        assert rm["retry_count"] == 2
        assert rm["cache_hit_rate"] == 0.75
        assert "tool_steps" in rm
        assert "parse_retry_count" in rm

    def test_runtime_metrics_all_keys_present(self, tmp_path):
        """runtime_metrics 所有 key 齐全。"""

        from src.state import RepairPlan, RepairState

        state = RepairState(issue_input="test")
        state.repair_run_id = "r-002"
        state.repair_plan = RepairPlan(issue_type="type_error")

        tracer = RepairRunTracer(str(tmp_path))
        tracer.run_id = "r-002"
        tracer.store.start_run_by_id("r-002")

        tracer.finalize(state, {"total_tokens": 500, "cache_hit_rate": 0.5})
        import json

        report = json.loads(
            (tracer.store.runs_dir / "r-002" / "report.json").read_text(encoding="utf-8")
        )
        rm = report["runtime_metrics"]
        required_keys = {
            "retry_count",
            "tool_steps",
            "parse_retry_count",
            "cache_hit_rate",
            "writes_used",
            "writes_limit",
            "shell_used",
            "shell_limit",
        }
        assert required_keys.issubset(set(rm.keys())), f"missing: {required_keys - set(rm.keys())}"
