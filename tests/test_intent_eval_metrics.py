"""Tests for offline intent eval metrics and online Prometheus recording."""

from __future__ import annotations

from agent_runtime.intent.eval_metrics import (
    IntentEvalCase,
    compute_intent_metrics,
    evaluate_case,
    load_eval_cases,
    run_intent_eval,
)
from agent_runtime.intent.models import RouteContext
from agent_runtime.intent.observability import record_intent_route
from agent_runtime.intent.router import IntentRouter
from agent_runtime.metrics import _reset_registry_for_tests, get_registry


class TestIntentEvalOffline:
    def test_load_cases_count(self):
        cases = load_eval_cases()
        assert len(cases) >= 35
        assert any("realistic" in c.tags for c in cases)

    def test_evaluate_known_help(self):
        row = evaluate_case(
            IntentEvalCase(id="h", text="/help", channel="repl", expect={"primary": "help"})
        )
        assert row.primary_ok
        assert row.predicted_primary == "help"

    def test_run_full_eval_has_enterprise_keys(self):
        report = run_intent_eval()
        s = report["summary"]
        for key in (
            "primary_accuracy",
            "misroute_rate",
            "severe_misroute_rate",
            "f1_macro",
            "f1_micro",
            "ece",
            "false_split_rate",
            "false_merge_rate",
            "clarify_precision",
            "overconfident_error_rate",
            "latency_ms_p50",
            "by_channel",
            "by_stratum",
            "weighted_misroute_rate",
            "in_distribution_misroute_rate",
            "heldout_gap_misroute_rate",
        ):
            assert key in s
        assert s["total"] >= 100
        assert s["heldout_gap_misroute_rate"] > 0  # stress set must stay hard
        assert any(c.stratum for c in load_eval_cases() if "realistic_user" in c.tags)
        assert "per_class" in report
        assert "confusion" in report

    def test_realistic_user_companion_loaded(self):
        cases = load_eval_cases()
        ru = [c for c in cases if "realistic_user" in c.tags]
        assert len(ru) >= 40
        strata = {c.stratum for c in ru}
        assert "ask_howto" in strata
        assert "vague_clarify" in strata
        assert "repair_stack" in strata
        assert "heldout_gap" in strata

    def test_compute_metrics_misroute(self):
        rows = [
            evaluate_case(
                IntentEvalCase(
                    id="a",
                    text="/help",
                    expect={"primary": "ask"},  # deliberately wrong label
                )
            )
        ]
        # predicted help, expected ask → misroute
        m = compute_intent_metrics(rows)
        assert m["summary"]["misroute_rate"] == 1.0
        assert m["summary"]["severe_misroute_rate"] >= 0.0


class TestIntentObservability:
    def setup_method(self):
        _reset_registry_for_tests()

    def teardown_method(self):
        _reset_registry_for_tests()

    def test_route_increments_prometheus(self):
        router = IntentRouter()
        router.route("/help", RouteContext(channel="repl"))
        rendered = get_registry().render()
        assert "fixloop_intent_routed_total" in rendered
        assert 'primary="help"' in rendered
        assert "fixloop_intent_latency_ms" in rendered

    def test_record_clarify_proxy(self):
        from agent_runtime.intent.graph import clarify_graph
        from agent_runtime.intent.models import IntentResult

        g = clarify_graph("x")
        result = IntentResult(
            primary="clarify",
            action="clarify",
            confidence=0.3,
            parser="rule",
            graph=g,
        )
        record_intent_route(result, RouteContext(channel="repl"), latency_ms=1.2)
        rendered = get_registry().render()
        assert "fixloop_intent_clarify_total" in rendered
        assert "fixloop_intent_misroute_proxy_total" in rendered
