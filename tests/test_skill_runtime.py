from __future__ import annotations

import time

import pytest

from agent_runtime.cancellation import CancellationToken
from agent_runtime.observability.prom_from_trace import event_category, record_canonical_event
from src.eval.skill_runtime_eval import (
    SkillExecutionEvalRow,
    execution_contract_metrics,
    outcome_ablation_metrics,
)
from src.skills.composition import SkillComposer, SkillStep
from src.skills.contract import (
    SideEffectLevel,
    SkillLifecycle,
    canonical_from_executable,
    validate_json_contract,
)
from src.skills.decision import build_canonical_skill_decision
from src.skills.executable_spec import ExecutableSkillSpec
from src.skills.execution import SkillExecutionGateway
from src.skills.feedback import SkillFeedbackLedger, SkillUsageEvent
from src.skills.invocation import SkillErrorCode
from src.skills.models import MatchedSkill
from src.skills.registry import CanonicalSkillRegistry, SkillRegistry
from src.skills.router import CandidateScore, RouteDecision


def _spec(name="demo", **updates):
    raw = {
        "name": name,
        "description": "demo",
        "input_schema": {
            "type": "object",
            "required": ["text"],
            "properties": {"text": {"type": "string"}},
            "additionalProperties": False,
        },
        "output_schema": {
            "type": "object",
            "required": ["result"],
            "properties": {"result": {"type": "object"}},
        },
        "completion_evidence": ["result.value"],
    }
    raw.update(updates)
    return ExecutableSkillSpec.model_validate(raw)


def _gateway(spec=None, **kwargs):
    return SkillExecutionGateway(SkillRegistry([spec or _spec()]), **kwargs)


def test_canonical_contract_hash_is_stable_and_semver_is_enforced():
    first = canonical_from_executable(_spec())
    second = canonical_from_executable(_spec())
    assert first.content_hash == second.content_hash
    assert len(first.content_hash) == 64
    with pytest.raises(ValueError):
        _spec(version="v1")


def test_schema_subset_rejects_required_type_and_extra_fields():
    schema = {
        "type": "object",
        "required": ["name"],
        "properties": {"name": {"type": "string"}},
        "additionalProperties": False,
    }
    assert validate_json_contract({}, schema) == ["$.name: required"]
    assert "expected string" in validate_json_contract({"name": 1}, schema)[0]
    assert "additional property" in validate_json_contract({"name": "x", "bad": 1}, schema)[0]


def test_gateway_rejects_invalid_input_before_runner():
    called = False

    def runner(args):
        nonlocal called
        called = True
        return {"result": {"value": 1}}

    result = _gateway().execute("demo", {}, runner=runner)
    assert not called
    assert result.invocation.error_code == SkillErrorCode.INPUT_INVALID.value


def test_read_only_failure_emits_fallback_but_side_effect_failure_does_not():
    soft_events = []
    _gateway(trace=lambda event, payload, status: soft_events.append(event)).execute("demo", {})
    assert "skill_fallback" in soft_events
    closed_events = []
    _gateway(
        _spec(side_effect_level="remote_write"),
        trace=lambda event, payload, status: closed_events.append(event),
    ).execute("demo", {})
    assert "skill_fallback" not in closed_events


def test_gateway_validates_output_and_completion_evidence():
    invalid = _gateway().execute("demo", {"text": "x"}, runner=lambda args: {})
    assert invalid.invocation.error_code == SkillErrorCode.OUTPUT_INVALID.value
    incomplete = _gateway().execute(
        "demo", {"text": "x"}, runner=lambda args: {"result": {}}
    )
    assert incomplete.invocation.error_code == SkillErrorCode.EVIDENCE_MISSING.value
    assert incomplete.invocation.status == "incomplete"


def test_gateway_success_creates_provenanced_observation(tmp_path):
    state = {}
    events = []
    gateway = _gateway(
        state=state,
        workspace_root=str(tmp_path),
        trace=lambda event, payload, status: events.append((event, status)),
    )
    result = gateway.execute(
        "demo",
        {"text": "sensitive"},
        runner=lambda args: {"result": {"value": "token=secret"}},
    )
    assert result.ok
    assert result.observation["provenance"]["invocation_id"] == result.invocation.invocation_id
    assert result.observation["redacted"] is True
    assert "token:[REDACTED]" in gateway.observations.expand(result.invocation.observation_id)
    assert [event for event, _ in events][-1] == "skill_completed"


def test_tools_require_runtime_admission():
    spec = _spec(allowed_tools=["probe"])

    def runner(args, probe=None):
        return {"result": {"value": probe({}) if probe else "no-tool"}}

    denied = _gateway(spec).execute(
        "demo", {"text": "x"}, runner=runner, tool_bindings={"probe": lambda args: "ok"}
    )
    assert denied.invocation.error_code == SkillErrorCode.PERMISSION_DENIED.value
    allowed = _gateway(spec).execute(
        "demo",
        {"text": "x"},
        runner=runner,
        tool_bindings={"probe": lambda args: "ok"},
        runtime_allowed_tools={"probe"},
    )
    assert allowed.ok
    assert allowed.output["result"]["value"] == "ok"


def test_tool_budget_is_runtime_enforced():
    spec = _spec(allowed_tools=["probe"], budget={"max_tool_calls": 1})

    def runner(args, probe=None):
        probe({})
        probe({})
        return {"result": {"value": 1}}

    result = _gateway(spec).execute(
        "demo",
        {"text": "x"},
        runner=runner,
        tool_bindings={"probe": lambda args: "ok"},
        runtime_allowed_tools={"probe"},
    )
    assert result.invocation.error_code == SkillErrorCode.TOOL_BUDGET_EXHAUSTED.value


def test_transient_runner_failure_retries_within_budget():
    attempts = 0

    def runner(args):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise ConnectionError("temporary")
        return {"result": {"value": 1}}

    result = _gateway(_spec(budget={"max_retries": 1})).execute(
        "demo", {"text": "x"}, runner=runner
    )
    assert result.ok
    assert result.invocation.retry_count == 1


def test_timeout_and_cancellation_have_stable_terminal_states():
    timed_out = _gateway().execute(
        "demo",
        {"text": "x"},
        runner=lambda args: (time.sleep(0.05) or {"result": {"value": 1}}),
        timeout_s=0.01,
    )
    assert timed_out.invocation.status == "timed_out"
    token = CancellationToken()
    token.cancel("test")
    cancelled = _gateway().execute(
        "demo", {"text": "x"}, runner=lambda args: {"result": {"value": 1}}, cancel_token=token
    )
    assert cancelled.invocation.status == "cancelled"


def test_untrusted_executable_is_guidance_only():
    result = _gateway(_spec(trust_level="untrusted")).execute(
        "demo", {"text": "x"}, runner=lambda args: {"result": {"value": 1}}
    )
    assert result.invocation.error_code == SkillErrorCode.PERMISSION_DENIED.value
    assert result.invocation.fail_closed is True


def test_side_effect_requires_idempotency_and_resume_reuses_result():
    spec = _spec(side_effect_level="local_write")
    gateway = _gateway(spec, state={})
    denied = gateway.execute(
        "demo", {"text": "x"}, runner=lambda args: {"result": {"value": 1}}
    )
    assert denied.invocation.error_code == SkillErrorCode.SIDE_EFFECT_UNCERTAIN.value
    first = gateway.execute(
        "demo",
        {"text": "x"},
        runner=lambda args: {"result": {"value": 1}},
        idempotency_key="write-1",
        read_before_write=True,
    )
    second = gateway.execute(
        "demo",
        {"text": "x"},
        runner=lambda args: pytest.fail("must not replay"),
        idempotency_key="write-1",
        read_before_write=True,
    )
    assert first.ok and second.reused
    assert first.invocation.invocation_id == second.invocation.invocation_id
    mismatch = gateway.execute(
        "demo",
        {"text": "different"},
        runner=lambda args: pytest.fail("must not replay"),
        idempotency_key="write-1",
        read_before_write=True,
    )
    assert mismatch.invocation.error_code == SkillErrorCode.SIDE_EFFECT_UNCERTAIN.value
    assert mismatch.invocation.status == "side_effect_uncertain"


def test_dry_run_never_injects_side_effect_tool():
    spec = _spec(side_effect_level="local_write", allowed_tools=["patch_file"])

    def runner(args, patch_file=None):
        assert patch_file is None
        return {"result": {"value": "planned"}}

    result = _gateway(spec).execute(
        "demo",
        {"text": "x"},
        runner=runner,
        tool_bindings={"patch_file": lambda args: pytest.fail("must not write")},
        runtime_allowed_tools={"patch_file"},
        dry_run=True,
    )
    assert result.ok


def test_preconditions_postconditions_and_receipt_are_enforced():
    spec = _spec(
        side_effect_level="remote_write",
        preconditions=["approval.id"],
        postconditions=["receipt"],
        input_schema={
            "type": "object",
            "required": ["text"],
            "properties": {
                "text": {"type": "string"},
                "approval": {"type": "object"},
            },
            "additionalProperties": False,
        },
    )
    missing_pre = _gateway(spec).execute(
        "demo", {"text": "x"}, idempotency_key="remote-1", runner=lambda args: {}
    )
    assert missing_pre.invocation.error_code == SkillErrorCode.INPUT_INVALID.value
    missing_post = _gateway(spec).execute(
        "demo",
        {"text": "x", "approval": {"id": "A-1"}},
        idempotency_key="remote-2",
        runner=lambda args: {"result": {"value": 1}},
    )
    assert missing_post.invocation.error_code == SkillErrorCode.EVIDENCE_MISSING.value
    complete = _gateway(spec).execute(
        "demo",
        {"text": "x", "approval": {"id": "A-1"}},
        idempotency_key="remote-3",
        runner=lambda args: {"result": {"value": 1}, "receipt": "PR-1"},
    )
    assert complete.ok
    assert complete.invocation.side_effect_receipt == "PR-1"


def test_version_pin_and_retired_lifecycle_are_enforced():
    registry = SkillRegistry([_spec(version="1.0.0")])
    registry.register_version(_spec(version="2.0.0"))
    pinned = SkillExecutionGateway(registry).execute(
        "demo",
        {"text": "x"},
        pinned_version="2.0.0",
        runner=lambda args: {"result": {"value": 2}},
    )
    assert pinned.ok and pinned.invocation.skill_version == "2.0.0"
    retired = _gateway(_spec(lifecycle="retired")).execute("demo", {"text": "x"})
    assert retired.invocation.error_code == SkillErrorCode.VERSION_UNAVAILABLE.value
    assert registry.verify_integrity("demo", "2.0.0", pinned.invocation.content_hash)


def test_canonical_decision_arbitrates_both_legacy_selectors():
    matched = MatchedSkill("guide", "python", ".*", 90)
    route = RouteDecision(
        "exec",
        "top1_margin",
        0.5,
        [CandidateScore("exec", 0.9, "keyword")],
        skill_version="1.0.0",
    )
    decision = build_canonical_skill_decision(matched, route)
    assert decision.selected == "exec"
    assert decision.guidance_skill == "guide"
    assert decision.selection_kind == "executable"


def test_composer_passes_structured_outputs_and_rejects_cycles():
    specs = [_spec("one"), _spec("two")]
    gateway = SkillExecutionGateway(SkillRegistry(specs))
    composer = SkillComposer(gateway)
    success = composer.run(
        [
            SkillStep("one", {"text": "a"}, runner=lambda args: {"result": {"value": 1}}),
            SkillStep(
                "two",
                input_mapper=lambda context, outputs: {"text": str(context["one"])},
                runner=lambda args: {"result": {"value": args["text"]}},
            ),
        ]
    )
    assert success.status == "succeeded"
    cycle = composer.run(
        [
            SkillStep("one", {"text": "a"}, runner=lambda args: {"result": {"value": 1}}),
            SkillStep("one", {"text": "b"}, runner=lambda args: {"result": {"value": 1}}),
        ]
    )
    assert cycle.reason == "composition_cycle"


def test_feedback_requires_evidence_for_strong_attribution():
    state = {}
    ledger = SkillFeedbackLedger(state)
    raw = ledger.record(
        SkillUsageEvent("demo", "1.0.0", "verified", outcome="helpful")
    )
    assert raw["outcome"] == "inconclusive"
    supported = ledger.record(
        SkillUsageEvent(
            "demo", "1.0.0", "verified", outcome="supported", evidence_refs=["OBS-1"]
        )
    )
    assert supported["outcome"] == "supported"
    unused = ledger.record_verification(
        skill_name="demo",
        skill_version="1.0.0",
        invocation_id="SKI-1",
        output_applied=False,
        verification_passed=False,
    )
    assert unused["outcome"] == "unused"
    verified = ledger.record_verification(
        skill_name="demo",
        skill_version="1.0.0",
        invocation_id="SKI-2",
        output_applied=True,
        verification_passed=True,
        evidence_refs=["OBS-VERIFY"],
    )
    assert verified["outcome"] == "supported"
    assert state["skill_usage_events"][-2]["stage"] == "output_applied"


def test_execution_and_outcome_metrics_cover_contract_and_ablation():
    contract = execution_contract_metrics(
        [SkillExecutionEvalRow("1", "succeeded", "succeeded", evidence_complete=True)]
    )
    assert contract["status_accuracy"] == 1.0
    cohorts = outcome_ablation_metrics(
        [
            {"cohort": "no_skill", "repair_succeeded": False, "tool_calls": 4},
            {"cohort": "correct_skill", "repair_succeeded": True, "tool_calls": 2},
        ]
    )
    assert cohorts["correct_skill"]["repair_success_rate"] == 1.0
    assert cohorts["no_skill"]["avg_tool_calls"] == 4


def test_side_effect_inference_for_builtin_tools():
    remote = canonical_from_executable(_spec(allowed_tools=["github_create_draft_pr"]))
    local = canonical_from_executable(_spec(allowed_tools=["patch_file"]))
    assert remote.side_effect_level is SideEffectLevel.REMOTE_WRITE
    assert local.side_effect_level is SideEffectLevel.LOCAL_WRITE
    assert remote.lifecycle is SkillLifecycle.ACTIVE


def test_canonical_registry_unifies_guidance_and_executable_contracts():
    executable = _spec("shared")
    guidance = type(
        "Guidance",
        (),
        {
            "name": "shared",
            "version": "1",
            "source": "workspace_local",
            "trust_level": "trusted",
            "scope": "workspace",
            "suggested_tools": [],
            "guidance": ["inspect evidence"],
            "avoid": [],
        },
    )()
    executable_registry = SkillRegistry([executable])
    catalog = type("Catalog", (), {"skills": [guidance]})()
    registry = CanonicalSkillRegistry.from_legacy(
        executable_registry=executable_registry, guidance_catalog=catalog
    )
    assert len(registry.list(name="shared")) == 2
    assert registry.resolve("shared", kind="executable").name == "shared"


def test_skill_trace_events_feed_low_cardinality_metrics():
    class Metrics:
        def __init__(self):
            self.calls = []

        def counter_inc(self, name, *, labels):
            self.calls.append((name, labels))

    metrics = Metrics()
    record_canonical_event(
        {
            "event": "skill_completed",
            "status": "ok",
            "payload": {"skill_name": "demo", "invocation_id": "high-cardinality"},
        },
        metrics,
    )
    assert event_category("skill_completed") == "skill"
    skill_metric = next(call for call in metrics.calls if call[0] == "fixloop_skill_events_total")
    assert skill_metric[1]["skill"] == "demo"
    assert "invocation_id" not in skill_metric[1]
