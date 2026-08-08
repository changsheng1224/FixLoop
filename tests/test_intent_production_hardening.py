"""Production controls for Intent Router fallback, risk and governance."""

from __future__ import annotations

import time

from agent_runtime.budget_manager import BudgetManager
from agent_runtime.intent.candidates import CandidateIntentCard, collect_user_feedback
from agent_runtime.intent.llm_fallback import maybe_refine, validate_llm_payload
from agent_runtime.intent.llm_runtime import IntentLlmPolicy, IntentLlmRuntime
from agent_runtime.intent.models import IntentGraph, IntentNode, RouteContext
from agent_runtime.intent.router import IntentRouter
from agent_runtime.intent.slo import evaluate_eval_slo, evaluate_route_slo
from agent_runtime.intent.taxonomy import ProposalStatus, proposal_from_candidate_card


class ReplyClient:
    def __init__(self, reply: str):
        self.reply = reply
        self.calls = 0

    def complete(self, _prompt, max_new_tokens=384):
        self.calls += 1
        return self.reply


class SlowClient:
    def complete(self, _prompt, max_new_tokens=384):
        time.sleep(0.2)
        return "{}"


def _weak_graph():
    return IntentGraph(
        nodes=[IntentNode("n0", "ask", "ask", confidence=0.2)],
        mode="single",
        root_ids=["n0"],
    )


def test_llm_runtime_honors_budget_and_timeout():
    denied = IntentLlmRuntime().complete(
        ReplyClient("{}"), "x", budget=BudgetManager({"llm_calls": 0.5})
    )
    assert denied.reason == "budget_exceeded"
    timed = IntentLlmRuntime(IntentLlmPolicy(timeout_s=0.05, max_retries=0)).complete(
        SlowClient(), "x"
    )
    assert timed.reason == "timeout"


def test_strict_llm_schema_rejects_extra_and_dangling_edges():
    payload = {
        "mode": "single",
        "nodes": [{"id": "n0", "primary": "ask", "role": "executable"}],
        "edges": [{"src": "n0", "dst": "missing", "kind": "sequence"}],
        "need_clarify": False,
        "unexpected": True,
    }
    errors = validate_llm_payload(payload)
    assert any(error.startswith("unknown_fields") for error in errors)
    assert "edge_0:invalid_endpoint" in errors


def test_invalid_llm_payload_fails_closed_to_clarify():
    reply = '{"mode":"single","nodes":[{"id":"n0","primary":"ask"}],"edges":[],"need_clarify":false,"x":1}'
    result = maybe_refine(_weak_graph(), "x", ReplyClient(reply), force=True)
    assert result.fallback_reason == "schema_rejected"
    assert result.graph.nodes[0].primary == "clarify"


def test_negative_scope_and_quoted_instruction_do_not_write():
    router = IntentRouter()
    negative = router.route(
        "不要修改任何文件，只告诉我为什么会 TypeError",
        RouteContext(channel="repl"),
    )
    assert negative.primary == "debug"
    assert "no_write" in negative.slots["constraints"]
    quoted = router.route(
        "用户说『帮我修』，但我只需要解释这句话",
        RouteContext(channel="repl"),
    )
    assert quoted.primary == "explain"


def test_high_risk_intent_uses_stricter_threshold():
    result = IntentRouter().route(
        "帮我修这个 bug",
        RouteContext(channel="repl", risk_thresholds={"high": 0.95}),
    )
    assert result.action == "clarify"
    assert result.raw_signals["risk_decision"]["risk"] == "high"


def test_route_emits_versions_thresholds_and_stage_timings():
    from agent_runtime.metrics import _reset_registry_for_tests, get_registry

    _reset_registry_for_tests()
    result = IntentRouter().route("解释 config.py", RouteContext(channel="repl"))
    signals = result.raw_signals
    assert signals["router_version"]
    assert signals["taxonomy_version"]
    assert signals["thresholds"]["tau_exec"] == 0.6
    assert signals["stage_latency_ms"]["total"] >= 0
    rendered = get_registry().render()
    assert "fixloop_intent_router_version_total" in rendered
    assert "fixloop_intent_stage_latency_ms" in rendered


def test_feedback_strength_and_taxonomy_review_gate():
    weak = collect_user_feedback(kind="cancel", text="stop", predicted="repair_request")
    confirmed = collect_user_feedback(
        kind="action_switch", text="explain", predicted="repair_request", chosen="explain"
    )
    assert weak.label_strength == "weak"
    assert confirmed.label_strength == "confirmed"
    card = CandidateIntentCard(
        key="gap:code_smell",
        label_hint="code_smell",
        count=25,
        confirmed_count=6,
        closest_existing="review",
    )
    proposal = proposal_from_candidate_card(card, confusion_rate=0.1)
    proposal.transition(ProposalStatus.EVIDENCE_READY.value)
    proposal.transition(ProposalStatus.REVIEWED.value)
    proposal.transition(ProposalStatus.APPROVED.value, reviewer="owner")
    assert proposal.status == "approved"


def test_slo_evaluation_reports_route_and_eval_violations():
    route = evaluate_route_slo(
        latency_ms=150,
        risk_decision={"risk": "high", "allow_execute": False},
        llm_runtime={"fallback_reason": "timeout"},
        embed_skipped=True,
        action="run_repair",
    )
    assert {"route_latency", "high_risk_below_threshold", "llm_fallback_degraded"} <= set(route)
    report = evaluate_eval_slo(
        {
            "severe_misroute_rate": 0.02,
            "clarify_recall": 0.8,
            "ece": 0.2,
            "exact_graph_match_rate": 0.7,
        }
    )
    assert report["passed"] is False
