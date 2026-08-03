"""Tests for confidence breakdown, clarify-only policy, same-sentence multi."""

from __future__ import annotations

from agent_runtime.intent.clarify import (
    is_ambiguous_utterance,
    normalize_clarify_reason,
)
from agent_runtime.intent.confidence import fuse_confidence, graph_confidence
from agent_runtime.intent.models import IntentGraph, IntentNode, RouteContext
from agent_runtime.intent.router import IntentRouter
from agent_runtime.intent.rules import (
    RuleHit,
    has_conflicting_leads,
    split_same_sentence_multi,
)
from agent_runtime.intent.segmenter import segment


def _route(text: str, **kwargs):
    return IntentRouter().route(text, RouteContext(channel="repl", **kwargs))


class TestSameSentenceMulti:
    def test_split_remember_repair_comma(self):
        parts = split_same_sentence_multi("请记住用 pytest，然后帮我修这个失败")
        assert parts is not None
        assert len(parts) == 2
        assert "记住" in parts[0] or "pytest" in parts[0]
        assert "修" in parts[1]

    def test_segment_expands_to_multi(self):
        segs = segment("请记住用 pytest，然后帮我修这个失败")
        assert len(segs) >= 2

    def test_router_same_sentence_multi(self):
        r = _route("请记住用 pytest，然后帮我修这个失败")
        assert r.graph.mode == "multi"
        execs = [n.primary for n in r.graph.nodes if n.role == "executable"]
        assert execs == ["remember", "repair_request"]
        assert r.raw_signals.get("intents")
        assert "c_graph" in r.confidence_breakdown

    def test_repair_then_remember_no_period(self):
        r = _route("帮我修这个 bug 并记住用 pytest")
        assert r.graph.mode == "multi"
        execs = [n.primary for n in r.graph.nodes if n.role == "executable"]
        assert execs == ["repair_request", "remember"]


class TestConflictClarify:
    def test_conflict_detect(self):
        assert has_conflicting_leads("帮我修这个 bug 同时重构一下 utils.py")

    def test_router_clarify_conflict(self):
        r = _route("帮我修这个 bug 同时重构一下 utils.py")
        assert r.primary == "clarify"
        assert r.action == "clarify"
        assert r.raw_signals.get("clarify_reason") == "conflict"
        assert r.raw_signals.get("allow_execute") is False
        assert r.raw_signals.get("fallback", {}).get("policy") == "clarify_only"


class TestClarifyOnly:
    def test_deixis_ambiguous(self):
        assert is_ambiguous_utterance("这个")
        assert is_ambiguous_utterance("修一下")
        r = _route("这个")
        assert r.action == "clarify"
        assert r.raw_signals.get("clarify_reason") in ("ambiguous", "unresolved_anaphora")
        assert "clarify_question" in r.slots

    def test_low_conf_forces_clarify_not_ask(self):
        # Force very high tau so even strong repair becomes clarify-only
        r = _route("帮我修这个 bug", tau_clarify=0.99, tau_exec=0.99)
        assert r.action == "clarify"
        assert r.raw_signals.get("allow_execute") is False
        assert normalize_clarify_reason(r.raw_signals["clarify_reason"]) in {
            "low_conf",
            "below_tau_exec",
        }

    def test_strong_repair_still_executes(self):
        r = _route("帮我修这个 bug")
        assert r.primary == "repair_request"
        assert r.action == "run_repair"
        assert r.raw_signals.get("allow_execute") is not False


class TestConfidence:
    def test_fuse_strong_rule(self):
        hit = RuleHit("remember", "promote_memory", 0.95, reason="rule:save_intent")
        conf, br = fuse_confidence(hit, embed_primary="ask", embed_score=0.8, embed_margin=0.2)
        assert conf >= 0.9
        assert br["c_rule"] == 0.95
        assert br.get("conflict") == 1.0

    def test_graph_confidence_multi(self):
        g = IntentGraph(
            nodes=[
                IntentNode(
                    id="n0",
                    primary="remember",
                    action="promote_memory",
                    confidence=0.9,
                    priority=1,
                ),
                IntentNode(
                    id="n1",
                    primary="repair_request",
                    action="run_repair",
                    confidence=0.8,
                    priority=0,
                ),
            ],
            mode="multi",
            root_ids=["n0"],
        )
        c, br = graph_confidence(g)
        assert 0.8 <= c <= 0.95
        assert br["min_node_conf"] == 0.8
        assert br["n_exec"] == 2.0
