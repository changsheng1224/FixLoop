"""Native 路径：正文里的 function_calls 应被执行，而非当 final。"""

from __future__ import annotations

from agent_runtime.canonical_protocol import parse_model_response
from agent_runtime.providers.clients import FakeNativeToolClient


def test_parser_extracts_function_calls_as_tool():
    text = (
        "<function_calls>\n"
        '<invoke name="read_file">\n'
        '<parameter name="path">a.py</parameter>\n'
        "</invoke>\n"
        "</function_calls>"
    )
    response = parse_model_response(text)
    call = response.payload["call"]
    assert response.response_kind == "tool_call"
    assert call.name == "read_file"
    assert call.arguments["path"] == "a.py"


def test_complete_turn_recovers_function_calls_in_text():
    fc = (
        "<function_calls>\n"
        '<invoke name="read_file">\n'
        '<parameter name="path">pkg/mod.py</parameter>\n'
        "</invoke>\n"
        "</function_calls>"
    )
    client = FakeNativeToolClient([fc, "<final>read ok</final>"])
    from agent_runtime.model_turn import FinishKind, ModelTurnRequest

    result = client.complete_turn(
        ModelTurnRequest(
            system_prompt="sys",
            messages=[{"role": "user", "content": "fix"}],
            tools=[
            {
                "name": "read_file",
                "description": "read",
                "input_schema": {"type": "object", "properties": {"path": {"type": "string"}}},
            }
            ],
        )
    )
    assert result.finish.kind == FinishKind.TOOL_CALLS
    assert result.tool_calls[0].name == "read_file"
    assert result.tool_calls[0].arguments == {"path": "pkg/mod.py"}


def test_native_rules_discourage_xml_function_calls():
    from agent_runtime.prompt_external import default_rules_text

    native = default_rules_text(native_tools=True)
    assert "禁止" in native and "function_calls" in native
    assert '<invoke name="' not in native
    text = default_rules_text(native_tools=False)
    assert "推荐使用格式B" in text
