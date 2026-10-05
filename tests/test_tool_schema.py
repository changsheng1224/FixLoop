"""tool_schema_view 单测。"""

from agent_runtime.tool_schema import tool_schema_view


class TestToolSchemaView:
    def test_excludes_run_pointer(self):
        registry = {
            "read_file": {
                "schema": {
                    "type": "object",
                    "properties": {"path": {"type": "string"}},
                    "required": ["path"],
                    "additionalProperties": False,
                },
                "description": "read",
                "risky": False,
                "run": lambda args: "secret",
            }
        }
        view = tool_schema_view(registry)
        assert "run" not in view["read_file"]
        assert view["read_file"]["schema"]["properties"] == {"path": {"type": "string"}}

    def test_sorted_iteration_not_required(self):
        registry = {
            "b": {
                "schema": {
                    "type": "object",
                    "properties": {},
                    "required": [],
                    "additionalProperties": False,
                },
                "description": "b",
            },
            "a": {
                "schema": {
                    "type": "object",
                    "properties": {},
                    "required": [],
                    "additionalProperties": False,
                },
                "description": "a",
            },
        }
        assert set(tool_schema_view(registry)) == {"a", "b"}
