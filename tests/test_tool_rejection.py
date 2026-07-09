"""tool_rejection 模块单测。"""

from agent_runtime.tool_rejection import (
    build_gateway_rejection_metadata,
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
