"""rejection_aggregate 单测。"""

import json

from src.repair.rejection_aggregate import (
    GATEWAY_AGENT_ERROR_PREFIX,
    aggregate_rejection_from_agent_reports,
    apply_gateway_denials_to_agent_errors,
    format_gateway_denial_summary,
    gateway_denial_count,
    merge_count_maps,
    summarize_repair_rejections,
)


class TestMergeCountMaps:
    def test_sums_by_key(self):
        assert merge_count_maps({"write_file": 2}, {"write_file": 1, "run_shell": 1}) == {
            "write_file": 3,
            "run_shell": 1,
        }


class TestAggregateRejection:
    def test_merges_across_agents(self):
        reports = {
            "localizer": {
                "permission_denied_by_tool": {"write_file": 2},
                "tool_rejections_by_layer": {"gateway": 2},
                "tool_rejections_by_gate": {"gateway": 2},
            },
            "retriever": {
                "permission_denied_by_tool": {"write_file": 1},
                "tool_rejections_by_layer": {"gateway": 1},
                "tool_rejections_by_gate": {"gateway": 1},
            },
        }
        summary = aggregate_rejection_from_agent_reports(reports)
        assert summary["permission_denied_by_tool"]["write_file"] == 3
        assert summary["tool_rejections_by_layer"]["gateway"] == 3
        assert summary["permission_denied_by_agent"]["localizer"]["write_file"] == 2

    def test_executor_only_excludes_permission_denied_by_tool(self):
        reports = {
            "patcher": {
                "tool_rejections_by_layer": {"executor": 1},
                "tool_rejections_by_gate": {"7": 1},
            },
        }
        summary = aggregate_rejection_from_agent_reports(reports)
        assert "permission_denied_by_tool" not in summary
        assert summary["tool_rejections_by_gate"]["7"] == 1

    def test_empty_reports(self):
        assert aggregate_rejection_from_agent_reports({}) == {}


class TestGatewayAgentErrors:
    def test_format_summary(self):
        assert format_gateway_denial_summary({"write_file": 2}) == (
            f"{GATEWAY_AGENT_ERROR_PREFIX} write_file×2"
        )
        assert format_gateway_denial_summary({"run_shell": 1, "write_file": 2}) == (
            f"{GATEWAY_AGENT_ERROR_PREFIX} run_shell×1, write_file×2"
        )

    def test_apply_sets_agent_errors(self):
        errors: dict = {}
        apply_gateway_denials_to_agent_errors(
            errors,
            {"localizer": {"write_file": 2}},
        )
        assert errors["localizer"] == f"{GATEWAY_AGENT_ERROR_PREFIX} write_file×2"

    def test_apply_merges_with_existing_error(self):
        errors = {"localizer": "parse failed"}
        apply_gateway_denials_to_agent_errors(
            errors,
            {"localizer": {"write_file": 1}},
        )
        assert errors["localizer"] == (f"parse failed; {GATEWAY_AGENT_ERROR_PREFIX} write_file×1")

    def test_apply_skips_duplicate_gateway_suffix(self):
        errors = {
            "localizer": f"{GATEWAY_AGENT_ERROR_PREFIX} write_file×2",
        }
        apply_gateway_denials_to_agent_errors(
            errors,
            {"localizer": {"write_file": 3}},
        )
        assert errors["localizer"] == f"{GATEWAY_AGENT_ERROR_PREFIX} write_file×2"

    def test_apply_no_op_when_empty(self):
        errors = {"patcher": "apply_failed"}
        apply_gateway_denials_to_agent_errors(errors, None)
        apply_gateway_denials_to_agent_errors(errors, {})
        assert errors == {"patcher": "apply_failed"}


class TestSummarizeRepairRejections:
    def test_reads_agent_report_files(self, tmp_path):
        run_dir = tmp_path / "run-1"
        run_dir.mkdir()
        (run_dir / "agent_report.localizer.json").write_text(
            json.dumps(
                {
                    "permission_denied_by_tool": {"write_file": 1},
                    "tool_rejections_by_layer": {"gateway": 1},
                    "tool_rejections_by_gate": {"gateway": 1},
                }
            ),
            encoding="utf-8",
        )
        summary = summarize_repair_rejections(run_dir)
        assert summary["permission_denied_by_tool"]["write_file"] == 1
        assert gateway_denial_count(summary) == 1
