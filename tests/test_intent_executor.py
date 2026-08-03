"""Tests for IntentGraphExecutor serial dispatch."""

from agent_runtime.intent.executor import IntentGraphExecutor
from agent_runtime.intent.models import (
    IntentEdge,
    IntentGraph,
    IntentNode,
    IntentResult,
    RouteContext,
)
from agent_runtime.intent.router import IntentRouter


def _result_multi():
    g = IntentGraph(
        nodes=[
            IntentNode(
                id="n0",
                primary="remember",
                action="promote_memory",
                role="executable",
                text="记住用 pytest",
                confidence=0.9,
                span={"start": 0, "end": 1},
            ),
            IntentNode(
                id="n1",
                primary="repair_request",
                action="run_repair",
                role="executable",
                text="修这个失败",
                confidence=0.9,
                span={"start": 10, "end": 11},
            ),
        ],
        edges=[IntentEdge(src="n0", dst="n1", kind="sequence")],
        mode="multi",
        root_ids=["n0"],
    )
    return IntentResult(
        primary="remember",
        action="run_graph",
        confidence=0.9,
        parser="rule",
        graph=g,
    )


class TestIntentGraphExecutor:
    def test_serial_order_remember_then_repair(self):
        order = []

        def remember(node):
            order.append("promote_memory")
            return True

        def repair(node):
            order.append("run_repair")
            return True

        report = IntentGraphExecutor(
            handlers={"promote_memory": remember, "run_repair": repair}
        ).serial(_result_multi())
        assert report.ok
        assert order == ["promote_memory", "run_repair"]

    def test_fail_fast(self):
        order = []

        def remember(node):
            order.append("promote_memory")
            raise RuntimeError("boom")

        def repair(node):
            order.append("run_repair")
            return True

        report = IntentGraphExecutor(
            handlers={"promote_memory": remember, "run_repair": repair}
        ).serial(_result_multi())
        assert report.aborted
        assert order == ["promote_memory"]
        assert "run_repair" not in order

    def test_router_multi_then_executor(self):
        order = []
        result = IntentRouter().route(
            "请记住用 pytest。然后帮我修这个失败。",
            RouteContext(channel="repl"),
        )
        assert result.action == "run_graph"

        IntentGraphExecutor(
            handlers={
                "promote_memory": lambda n: order.append("promote_memory"),
                "run_repair": lambda n: order.append("run_repair"),
            }
        ).serial(result)
        assert order == ["promote_memory", "run_repair"]
