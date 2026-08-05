"""Retriever 终态工具 submit_retrieved_context + AgentLoop 短路。"""

from __future__ import annotations

import json

from src.tools.submit_retrieved_context import (
    SUBMIT_RETRIEVED_CONTEXT,
    args_to_retrieved_context,
    submit_retrieved_context,
)


class TestSubmitRetrievedContextTool:
    def test_rejects_empty_related_tests(self):
        out = submit_retrieved_context({"related_tests": []})
        assert out.startswith("Error")

    def test_serializes_context(self):
        out = submit_retrieved_context(
            {
                "related_tests": ["tests/test_a.py::test_x"],
                "caller_locations": ["a.py:1"],
            }
        )
        data = json.loads(out)
        assert data["related_tests"] == ["tests/test_a.py::test_x"]
        assert data["caller_locations"] == ["a.py:1"]

    def test_args_to_context_coerces_strings(self):
        ctx = args_to_retrieved_context(
            {"related_tests": "tests/t.py::t", "similar_snippets": ["snip"]}
        )
        assert ctx.related_tests == ["tests/t.py::t"]
        assert ctx.similar_snippets == [{"text": "snip"}]

    def test_registered_and_terminal(self, tmp_path):
        from agent_runtime.tool_context import ToolContext
        from src.tools.composite import REPAIR_CANONICAL_TOOL_NAMES, build_repair_canonical_tools

        assert SUBMIT_RETRIEVED_CONTEXT in REPAIR_CANONICAL_TOOL_NAMES
        tools = build_repair_canonical_tools(ToolContext(root=str(tmp_path)))
        spec = tools[SUBMIT_RETRIEVED_CONTEXT]
        assert spec.get("terminal") is True
        assert "related_tests" in spec["schema"]

    def test_gateway_allows_retriever_only(self):
        from src.middleware import build_repair_gateway

        gw = build_repair_gateway()
        assert gw.can_call("retriever", SUBMIT_RETRIEVED_CONTEXT)
        assert not gw.can_call("localizer", SUBMIT_RETRIEVED_CONTEXT)
        assert not gw.can_call("patcher", SUBMIT_RETRIEVED_CONTEXT)


class TestTerminalToolLoop:
    def test_native_submit_ends_with_payload(self, tmp_path):
        from agent_runtime.config import AgentConfig
        from agent_runtime.providers.clients import FakeNativeToolClient
        from agent_runtime.runtime import Agent
        from agent_runtime.tool_context import ToolContext
        from agent_runtime.workspace import WorkspaceContext
        from src.middleware import build_repair_gateway
        from src.tools.composite import build_repair_agent_tools

        ws = WorkspaceContext.build(str(tmp_path))
        (tmp_path / "a.py").write_text("x=1\n", encoding="utf-8")
        tools = build_repair_agent_tools(ToolContext(root=str(tmp_path)), "retriever")
        gw = build_repair_gateway(str(tmp_path))
        payload = {
            "name": SUBMIT_RETRIEVED_CONTEXT,
            "args": {"related_tests": ["tests/test_a.py::test_x"]},
        }
        client = FakeNativeToolClient([f"<tool>{json.dumps(payload)}</tool>"])
        agent = Agent(
            config=AgentConfig(provider="fake", max_steps=4, approval="auto"),
            model_client=client,
            workspace=ws,
            cwd=str(tmp_path),
            tools=tools,
            system_prompt="retriever",
            agent_name="retriever",
            tool_dispatch=gw.dispatch,
        )
        answer = agent.ask("retrieve context", skip_plan=True)
        data = json.loads(answer)
        assert data["related_tests"] == ["tests/test_a.py::test_x"]

    def test_parse_tool_call_not_final_message(self):
        from src.repair.output_parsers import parse_retrieved_context

        ctx = parse_retrieved_context(
            '<function_calls>\n<invoke name="search">\n</invoke>\n</function_calls>'
        )
        assert ctx.related_tests == []

    def test_parse_step_limit_is_agent_incomplete_not_json_object(self, caplog):
        import logging

        from src.repair.output_parsers import parse_retrieved_context

        msg = "<final>已达到最大工具调用步数限制(4)，当前任务未完成。</final>"
        with caplog.at_level(logging.INFO, logger="fixloop.output_parsers"):
            ctx = parse_retrieved_context(msg)
        assert ctx.related_tests == []
        assert not any("不是 JSON 对象" in r.message for r in caplog.records)
        assert any("agent_incomplete" in r.message for r in caplog.records)
        assert not any(r.levelno >= logging.WARNING for r in caplog.records)

    def test_parse_empty_is_soft(self, caplog):
        import logging

        from src.repair.output_parsers import parse_retrieved_context

        with caplog.at_level(logging.INFO, logger="fixloop.output_parsers"):
            parse_retrieved_context("")
        assert any("empty_response" in r.message for r in caplog.records)
        assert not any(r.levelno >= logging.WARNING for r in caplog.records)

    def test_retriever_factory_disables_json_mode(self, tmp_path):
        from agent_runtime.providers.clients import FakeModelClient
        from agent_runtime.workspace import WorkspaceContext
        from src.agents.factory import create_retriever

        ws = WorkspaceContext.build(str(tmp_path))
        agent = create_retriever(FakeModelClient([]), ws, cwd=str(tmp_path))
        assert agent.config.json_mode is False
        assert agent.config.max_steps >= 6
