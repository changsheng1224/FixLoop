"""tool_rejection 模块单测。"""

from agent_runtime.tool_rejection import (
    build_gateway_rejection_metadata,
    build_rejection_observability_payload,
    tool_trace_payload,
)


class TestToolRejection:
    def test_gateway_metadata(self):
        meta = build_gateway_rejection_metadata()
        assert meta["rejection_layer"] == "gateway"
        assert meta["tool_error_code"] == "permission_denied"

    def test_tool_trace_payload_filters_keys(self):
        payload = tool_trace_payload(
            "write_file",
            {
                "tool_status": "rejected",
                "rejection_layer": "gateway",
                "run": "secret",
            },
        )
        assert payload == {
            "tool": "write_file",
            "tool_status": "rejected",
            "rejection_layer": "gateway",
        }

    def test_observability_payload_flat_metrics_for_grafana(self):
        payload = build_rejection_observability_payload(
            {
                "tool_rejections_by_layer": {"gateway": 2, "executor": 1},
                "tool_rejections_by_gate": {"gateway": 2, "3": 1},
                "permission_denied_by_tool": {"write_file": 2},
            }
        )
        assert payload["gateway_denials"] == 2
        assert payload["tool_rejections_by_gate"] == {"gateway": 2, "3": 1}
        assert payload["tool_rejection_metrics"] == [
            {"layer": "executor", "gate_id": "3", "count": 1},
            {"layer": "gateway", "gate_id": "gateway", "count": 2},
        ]

    def test_observability_payload_empty(self):
        assert build_rejection_observability_payload({}) == {}
