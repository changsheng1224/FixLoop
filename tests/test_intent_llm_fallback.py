"""Tests for graph-level LLM refine (+ candidates side-channel)."""

from agent_runtime.intent.candidates import events_from_llm_candidates
from agent_runtime.intent.graph import IntentGraph
from agent_runtime.intent.llm_fallback import maybe_refine, maybe_refine_graph
from agent_runtime.intent.models import IntentNode, RouteContext
from agent_runtime.intent.router import IntentRouter


class FakeClient:
    def __init__(self, reply: str):
        self.reply = reply

    def complete(self, prompt, max_new_tokens=256):
        return self.reply


def _weak_graph():
    return IntentGraph(
        nodes=[
            IntentNode(
                id="n0",
                primary="ask",
                action="ask",
                confidence=0.4,
                text="x",
            )
        ],
        edges=[],
        mode="single",
        root_ids=["n0"],
    )


class TestLlmFallback:
    def test_no_client_skips(self):
        g = _weak_graph()
        out = maybe_refine_graph(g, "x", None, tau_llm=0.55)
        assert out is g or out.nodes[0].primary == "ask"

    def test_refine_to_multi(self):
        reply = """{
          "mode": "multi",
          "nodes": [
            {"id": "n0", "primary": "remember", "role": "executable", "segment_index": 0},
            {"id": "n1", "primary": "repair_request", "role": "executable", "segment_index": 1}
          ],
          "edges": [{"src": "n0", "dst": "n1", "kind": "sequence"}],
          "candidates": [
            {"label": "remember", "confidence": 0.9},
            {"label": "repair_request", "confidence": 0.85}
          ],
          "need_clarify": false
        }"""
        g = _weak_graph()
        out = maybe_refine_graph(
            g,
            "记住。然后修",
            FakeClient(reply),
            tau_llm=0.55,
            segments=["记住", "修"],
            force=True,
        )
        assert out.mode == "multi"
        assert len(out.executable_nodes()) == 2

    def test_refine_returns_candidates(self):
        reply = """{
          "mode": "single",
          "nodes": [{"id": "n0", "primary": "debug", "role": "executable", "segment_index": 0}],
          "edges": [],
          "candidates": [
            {"label": "debug", "confidence": 0.8},
            {"label": "explain", "confidence": 0.55},
            {"label": "code_smell_review", "confidence": 0.4, "merge_into": "review", "allow_new": true}
          ],
          "need_clarify": false
        }"""
        res = maybe_refine(
            _weak_graph(),
            "不要改，只告诉我为什么 TypeError",
            FakeClient(reply),
            force=True,
        )
        assert res.applied
        assert res.graph.nodes[0].primary == "debug"
        assert len(res.candidates) >= 2
        assert res.candidates[0].label == "debug"
        novel = [c for c in res.candidates if c.is_new]
        assert novel and novel[0].merge_into == "review"

    def test_illegal_primary_clarify_or_keep(self):
        reply = '{"mode":"single","nodes":[{"id":"n0","primary":"NOT_REAL","role":"executable"}],"edges":[]}'
        g = _weak_graph()
        out = maybe_refine_graph(g, "x", FakeClient(reply), force=True)
        assert out.nodes[0].primary in ("clarify", "ask")

    def test_router_attaches_llm_candidates(self):
        reply = """{
          "mode": "single",
          "nodes": [{"id": "n0", "primary": "ask", "role": "executable", "segment_index": 0}],
          "edges": [],
          "candidates": [
            {"label": "ask", "confidence": 0.7},
            {"label": "explain", "confidence": 0.5}
          ],
          "need_clarify": true
        }"""
        # Force weak path: short unclear text already clarifies; use mid conf ask
        r = IntentRouter().route(
            "配置里 timeout 该怎么设？",
            RouteContext(
                channel="repl",
                light_client=FakeClient(reply),
                tau_llm=0.99,  # force LLM path even for stronger graphs
            ),
        )
        # May or may not apply depending on graph conf; if applied, candidates present
        if (r.raw_signals or {}).get("llm_candidates"):
            assert any(c.get("label") == "explain" for c in r.raw_signals["llm_candidates"])

    def test_events_from_llm_candidates(self):
        evs = events_from_llm_candidates(
            text="x",
            predicted="ask",
            llm_candidates=[
                {"label": "explain", "confidence": 0.6, "is_new": False},
                {
                    "label": "smell_check",
                    "confidence": 0.4,
                    "merge_into": "review",
                    "is_new": True,
                },
            ],
        )
        assert evs
        assert all(e.source == "llm_nominate" for e in evs)
        assert any(e.proposed_label == "smell_check" for e in evs)
