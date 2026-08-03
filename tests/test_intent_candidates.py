"""Tests for candidate intent discovery."""

from __future__ import annotations

import tempfile
from pathlib import Path

from agent_runtime.intent.candidates import (
    CandidateStore,
    aggregate_cards,
    collect_from_route,
    collect_user_feedback,
    nominate_with_llm,
)
from agent_runtime.intent.models import RouteContext
from agent_runtime.intent.router import IntentRouter


class TestCollectFromRoute:
    def test_conflict_emits_event(self):
        r = IntentRouter().route(
            "帮我修这个 bug 同时重构一下 utils.py",
            RouteContext(channel="repl"),
        )
        events = collect_from_route(r, text="帮我修这个 bug 同时重构一下 utils.py")
        assert events
        assert any(e.source in ("conflict", "clarify_residual") for e in events)

    def test_router_attaches_candidate_signals(self):
        root = Path(tempfile.mkdtemp(prefix="intent_cand_"))
        r = IntentRouter().route(
            "这个",
            RouteContext(channel="repl", candidate_root=str(root)),
        )
        assert r.action == "clarify"
        assert r.raw_signals.get("candidate_keys") or r.raw_signals.get("candidate_events")
        assert len(CandidateStore(root).load()) >= 1


class TestAggregateAndFeedback:
    def test_aggregate_cards(self):
        root = Path(tempfile.mkdtemp(prefix="intent_cand_agg_"))
        store = CandidateStore(root)
        for _ in range(3):
            store.append(
                collect_user_feedback(
                    kind="cancel",
                    text="别改了",
                    predicted="repair_request",
                )
            )
        cards = aggregate_cards(store.load())
        assert cards
        assert cards[0].count >= 3
        assert cards[0].severity_max == "high"

    def test_rephrase_event(self):
        ev = collect_user_feedback(
            kind="rephrase",
            text="帮我解释 config.py 的 timeout 逻辑",
            predicted="ambiguous",
            previous_text="这个",
        )
        assert ev.source == "user_rephrase"
        assert "prev=" in ev.note


class TestDiscoverFromCases:
    def test_from_eval_smoke(self):
        from agent_runtime.intent.candidates import discover_from_cases

        events, cards = discover_from_cases(
            root=None,
            persist=False,
            strata=["heldout_gap", "vague_clarify"],
        )
        assert len(events) >= 1
        assert len(cards) >= 1

    def test_nominate_merge(self):
        class Fake:
            def complete(self, _prompt: str) -> str:
                return '{"merge_into": "explain", "proposed_label": null, "reason": "how-to"}'

        ev = nominate_with_llm("这段代码啥意思", light_client=Fake())
        assert ev is not None
        assert ev.source == "llm_nominate"
        assert ev.merge_into == "explain"
        assert ev.proposed_label is None

    def test_nominate_none_without_client(self):
        assert nominate_with_llm("x", light_client=None) is None
