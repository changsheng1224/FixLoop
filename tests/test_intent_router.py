"""Gold-sample tests for IntentRouter (rules+planner; embed mocked)."""

from __future__ import annotations

from agent_runtime.intent.models import RouteContext
from agent_runtime.intent.router import IntentRouter


def _route(text: str, *, channel: str = "repl", **kwargs):
    router = IntentRouter()
    ctx = RouteContext(channel=channel, **kwargs)  # type: ignore[arg-type]
    return router.route(text, ctx)


class TestIntentRouterGold:
    def test_type_error_issue_single_repair(self):
        text = (
            'Traceback (most recent call last):\n'
            '  File "calculator.py", line 42, in add\n'
            "TypeError: unsupported operand type(s)"
        )
        r = _route(text, channel="repair")
        assert r.graph.mode in ("single", "hybrid")
        execs = [n for n in r.graph.nodes if n.role == "executable"]
        assert len(execs) == 1
        assert execs[0].primary == "repair_issue"
        assert "calculator.py" in r.slots.get("suspect_files", [])

    def test_hybrid_constraint_attach(self):
        r = _route("帮我修这个 TypeError。只用改 foo.py", channel="repl")
        assert r.graph.mode == "hybrid"
        execs = [n for n in r.graph.nodes if n.role == "executable"]
        assert len(execs) == 1
        assert execs[0].primary == "repair_request"
        assert any(e.kind == "constrains" for e in r.graph.edges)
        assert "foo.py" in execs[0].slots.get("suspect_files", []) or "foo.py" in str(
            execs[0].slots
        )

    def test_multi_remember_then_repair(self):
        r = _route("请记住用 pytest。然后帮我修这个失败。", channel="repl")
        assert r.graph.mode == "multi"
        execs = [n for n in r.graph.nodes if n.role == "executable"]
        assert len(execs) == 2
        assert execs[0].primary == "remember"
        assert execs[1].primary == "repair_request"
        assert any(e.kind in ("sequence", "depends_on") for e in r.graph.edges)
        assert r.action == "run_graph"

    def test_pure_stack_not_multi(self):
        text = (
            'Traceback (most recent call last):\n'
            '  File "a.py", line 1\n'
            '  File "b.py", line 2\n'
            "TypeError: x"
        )
        r = _route(text, channel="repair")
        execs = [n for n in r.graph.nodes if n.role == "executable"]
        assert len(execs) == 1
        assert execs[0].primary == "repair_issue"

    def test_two_asks_merge(self):
        r = _route("这个函数是干什么的？另外 AgentLoop 呢？", channel="repl")
        execs = [n for n in r.graph.nodes if n.role == "executable"]
        assert len(execs) == 1
        assert execs[0].primary == "explain"
        assert r.graph.mode == "single"

    def test_enterprise_implement(self):
        r = _route("实现一个简单的 rate limiting middleware", channel="repl")
        assert r.primary == "implement"
        assert r.action == "run_implement"

    def test_slash_help(self):
        r = _route("/help")
        assert r.primary == "help"
        assert r.action == "help"

    def test_embed_unavailable_still_works(self):
        r = _route("配置里 timeout 该怎么设？", channel="repl")
        assert r.primary == "ask"

    def test_emit_intent_routed(self):
        events = []

        def emit(name, payload):
            events.append((name, payload))

        r = _route("帮我修这个 bug", channel="repl", emit=emit)
        assert r.primary == "repair_request"
        assert events and events[0][0] == "intent_routed"
        assert "mode" in events[0][1]


class TestIntentRouterEmbedConflict:
    def test_strong_rule_beats_embed(self):
        # embed always says ask; rule says remember
        def embed_fn(_text: str):
            return [1.0, 0.0]  # unused dims; EmbedIndex needs prototypes

        from agent_runtime.intent.embed_index import EmbedIndex

        prototypes = {
            "ask": ["x"],
            "remember": ["y"],
        }

        def embed_fn2(text: str):
            # always closer to ask
            if text == "x":
                return [1.0, 0.0]
            if text == "y":
                return [0.0, 1.0]
            return [1.0, 0.0]

        router = IntentRouter(embed_index=EmbedIndex(prototypes, embed_fn=embed_fn2))
        r = router.route(
            "请记住用 pytest",
            RouteContext(channel="repl", embed_fn=embed_fn2),
        )
        assert r.primary == "remember"
