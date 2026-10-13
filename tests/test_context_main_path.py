"""S5: controlled providers, real repair tools, host pytest and durable state."""

import hashlib
import json
from copy import deepcopy

import pytest

from agent_runtime.checkpoint import create_checkpoint
from agent_runtime.context_manager import ContextManager
from agent_runtime.context_preparation import request_hash
from agent_runtime.errors import ContextBuildBlockedError
from agent_runtime.history_summary import SUMMARY_INSTRUCTIONS
from agent_runtime.message_projection import init_run_projection, seal_history_at_build
from agent_runtime.plan_runtime.constraint_records import constraint_history
from agent_runtime.plan_runtime.decisions import decision_history
from agent_runtime.task_extraction_schema import INSTRUCTIONS
from agent_runtime.task_state import TaskState
from src.repair.plan_binding import RepairPlanBinding
from src.state import RepairState
from tests.history_summary_support import summary_json
from tests.test_exploration_runtime import REQUESTS, DiscoveryClient, finish
from tests.test_history_summary import agent_at
from tests.test_task_extraction import anchor, output, rule
from tests.test_task_extraction_l2 import automatic_fixture


def configure(agent):
    agent.config.prompt_budget = 8000
    agent.config.hard_cap = 8000
    agent.config.model_context_window = 16384
    agent.config.max_new_tokens = 2048
    agent.config.context_safety_margin = 256
    agent.config.budget.output_tokens = 100000
    agent.config.budget.total_tokens = 200000
    agent.config.budget.max_llm_calls = 30


@pytest.mark.parametrize("native", [False, True])
def test_compressed_repair_requests_and_restart_share_authority_and_budget(
    tmp_path, monkeypatch, native
):
    from agent_runtime import context_manager

    orch, state, client, candidates = automatic_fixture(tmp_path, native=native)
    agent = orch.patcher
    configure(agent)
    orch.lifecycle.initialize_model_budget(None, 0)
    summary_prompts = []

    class SummaryClient:
        def complete(self, prompt, **kwargs):
            if prompt.startswith((INSTRUCTIONS, "Create a small repair task DAG")):
                return client.complete(prompt, **kwargs)
            if prompt.startswith(SUMMARY_INSTRUCTIONS):
                summary_prompts.append(prompt)
                return summary_json("Old hypothesis only; current constraints remain authoritative")
            return state.issue_input

    agent.light_client = SummaryClient()
    for i in range(14):
        agent.record({"role": "user", "content": f"FAILED historical-{i} " + "details " * 20})
    pipeline = context_manager.run_compression_pipeline

    def force_summary(*args, **kwargs):
        kwargs["l5_trigger_tokens"] = 1
        return pipeline(*args, **kwargs)

    monkeypatch.setattr(context_manager, "run_compression_pipeline", force_summary)
    captured = []
    validate = ContextManager.validate_prepared_request

    def capture(manager, request, metadata):
        validate(manager, request, metadata)
        if manager.agent is agent:
            context = metadata["long_task_context"]
            manifest = metadata["context_manifest"]
            assert manifest["request_hash"] == request_hash(request, "native" if native else "xml")
            assert (
                manifest["required_state_ref"]["task_context_revision"]
                == context["context_revision"]
            )
            assert context["hard_constraints"] == state.hard_constraints == ["Do not modify tests"]
            assert manifest["constraint_refs"][0]["status"] == "active"
            assert manifest["model_limits"]["max_output_tokens"] == request.max_output_tokens
            assert manifest["provider_input_tokens"] <= manifest["model_limits"]["input_limit"]
            captured.append((deepcopy(request), deepcopy(metadata)))

    monkeypatch.setattr(ContextManager, "validate_prepared_request", capture)
    original_tests = (tmp_path / "test_value.py").read_bytes()
    try:
        patches, meta = orch.patcher_execution.run_patcher_toolized(
            state, "Read and fix value.py", {}
        )
        assert patches, (meta, state.agent_errors)
        binding = orch.plan_binding
        assert orch.verification.run_verifier(state).all_passed
        assert captured and summary_prompts and (len(candidates) == 2)
        assert any((m["context_manifest"]["decision_refs"] for _, m in captured))
        assert any((m["context_manifest"]["evidence_consumption"] for _, m in captured))
        assert any((m["compression_pipeline"].get("l5") for _, m in captured))
        if native:
            assert [r for r, _ in captured] == client.requests
        else:
            assert all((r.messages[0]["content"] in client.prompts for r, _ in captured))
        checkpoint = create_checkpoint(agent, TaskState.create(user_request="continue"), "continue")
        for field in ("constraint_refs", "decision_refs", "model_limits", "history_compression"):
            assert checkpoint["context_manifest"][field] == agent.session["context_manifest"][field]
        authoritative = deepcopy(binding.session.long_task_state.to_dict())
        writes = len(binding.session.store.latest("operation", "operation_id"))
        attempts = len(binding.session.store.latest("attempt", "attempt_id"))
        orch.lifecycle.release_model_budget(state)
        saved_budget = deepcopy(state.node_timings["model_budget"])
        assert {c["purpose"] for c in saved_budget["model_calls"].values()} >= {
            "extraction",
            "planning",
            "summary",
            "patcher",
        }
        saved_state = state.to_dict()
    finally:
        if orch.plan_binding:
            orch.plan_binding.close()
        if orch.model_budget is not None:
            orch.lifecycle.release_model_budget(state)
    resumed, _, resumed_client, resumed_candidates = automatic_fixture(
        tmp_path, native=native, resume=True
    )
    configure(resumed.patcher)
    restored = RepairState.from_dict(saved_state)
    restored.hard_constraints = ["Wrong checkpoint mirror: edit tests"]
    resumed.patcher.session["long_task_context"] = {"original_request": "wrong cached goal"}
    resumed.lifecycle.initialize_model_budget(saved_state, 0)
    try:
        patches, meta = resumed.patcher_execution.run_patcher_toolized(restored, "continue", {})
        assert patches and meta["edit_mode"] == "plan_resume_adopted"
        assert resumed.verification.run_verifier(restored).all_passed
        binding = resumed.plan_binding
        current = binding.session.long_task_state.to_dict()
        for field in (
            "original_request",
            "constraint_records",
            "key_decisions",
            "context_revision",
        ):
            assert current[field] == authoritative[field]
        assert restored.hard_constraints == ["Do not modify tests"]
        assert len(binding.session.store.latest("operation", "operation_id")) == writes
        assert len(binding.session.store.latest("attempt", "attempt_id")) == attempts
        assert resumed.model_budget.snapshot() == saved_budget
        assert not resumed_client.prompts and (not resumed_candidates)
        assert (tmp_path / "test_value.py").read_bytes() == original_tests
        assert (tmp_path / "value.py").read_text() == "def answer():\n    return 2\n"
    finally:
        if resumed.plan_binding:
            resumed.plan_binding.close()
        resumed.lifecycle.release_model_budget(restored)


def test_host_add_and_revoke_invalidate_completed_children_without_reexecution(tmp_path):

    def respond(data, call):
        message = data["source"]["message_id"]
        if message == "original-request":
            return output([rule(data, "Do not modify tests", "Do not modify tests.")])
        if message == "new-choice":
            return output(
                [
                    rule(data, "Preserve public API"),
                    {
                        "kind": "decision",
                        "action": "add",
                        "statement": "Use existing API",
                        "node_id": data["state"]["nodes"][0]["node_id"],
                        "source_refs": [anchor(data)],
                    },
                ]
            )
        constraint = next(
            c for c in data["state"]["constraints"] if c["statement"] == "Preserve public API"
        )
        decision = data["state"]["decisions"][0]
        return output(
            [
                rule(
                    data,
                    "Preserve public API",
                    action="revoke",
                    constraint_id=constraint["constraint_id"],
                    expected_revision=constraint["revision"],
                ),
                {
                    "kind": "decision",
                    "action": "revoke",
                    "decision_id": decision["decision_id"],
                    "expected_revision": decision["revision"],
                    "node_id": decision["node_id"],
                    "source_refs": [anchor(data)],
                },
            ]
        )

    orch, state, _, candidates = automatic_fixture(tmp_path, respond=respond)
    children = []

    def child_factory(task):
        child = DiscoveryClient(task)
        children.append(child)
        return child

    orch.exploration_client_factory = child_factory
    binding = RepairPlanBinding(orch, state)
    orch.plan_binding = binding
    runtime = binding.exploration
    try:
        node = binding.session.plan.nodes[0]
        binding.session.local.attempt = binding.session.prepare(node.node_id)
        first = runtime.delegate(REQUESTS[:1])[0]["handle"]
        assert finish(runtime, [first])["findings"]
        binding.accept_user_message("new-choice", "Preserve public API; use existing API")
        current = runtime.collect([first])
        assert not current["findings"]
        assert current["tasks"][0]["validation"]["reason"] == "task_context_changed"
        assert state.hard_constraints == ["Do not modify tests", "Preserve public API"]
        second = runtime.delegate(REQUESTS[:1])[0]["handle"]
        assert finish(runtime, [second])["findings"]
        context = runtime.tasks()[-1].payload["exploration"]["delegation_context"]
        assert context["hard_constraints"] == state.hard_constraints
        assert (
            context["key_decisions"]
            and context["task_context_revision"] == binding.session.long_task_state.context_revision
        )
        binding.accept_user_message(
            "revoke-choice", "Revoke Preserve public API and the existing API choice"
        )
        assert not runtime.collect([second])["findings"]
        assert state.hard_constraints == ["Do not modify tests"]
        assert all(
            d["status"] == "revoked"
            for d in decision_history(binding.session.long_task_state.key_decisions)[1].values()
        )
        _, metadata = ContextManager(orch.patcher).prepare_request(
            "continue", protocol="xml", max_output_tokens=2048
        )
        assert any(
            r["status"] == "revoked" and r["revision"] == 2 for r in metadata["constraint_refs"]
        )
        assert not metadata["long_task_context"]["decisions"]
        assert metadata["decision_refs"][0]["status"] == "revoked"
        assert len(candidates) == 3 and [len(c.requests) for c in children] == [2, 2]
        assert any(
            r["status"] == "revoked"
            for r in constraint_history(binding.session.long_task_state.constraint_records)[
                1
            ].values()
        )
        recorded = binding.session.invoke(
            binding.session.local.attempt,
            lambda attempt: binding._isolated_read(
                {"tool": node.tool_name, "arguments": json.loads(node.arguments_json)}, attempt
            ),
        )
        assert binding.session.settle(recorded) == "succeeded"
    finally:
        binding.session.local.attempt = None
        binding.close()


def test_host_constraint_mirror_updates_even_when_followup_resolution_blocks(tmp_path, monkeypatch):

    def respond(data, call):
        statement = "Keep tests" if call == 1 else "Preserve API"
        return output([rule(data, statement)])

    orch, state, _, _ = automatic_fixture(tmp_path, respond=respond)
    binding = RepairPlanBinding(orch, state)
    try:

        def block():
            raise ContextBuildBlockedError("clarification_required")

        monkeypatch.setattr(binding.extraction, "resume_pending", block)
        with pytest.raises(ContextBuildBlockedError, match="clarification_required"):
            binding.accept_user_message("new-rule", "Preserve API")
        assert state.hard_constraints == ["Keep tests", "Preserve API"]
        assert state.hard_constraints == binding.session.long_task_state.hard_constraints
    finally:
        binding.close()


@pytest.mark.parametrize("keep_history", [False, True])
def test_manifest_records_only_final_history_units_and_other_selected_forms(tmp_path, keep_history):
    manager = ContextManager(agent_at(tmp_path))
    unit = "same complete interaction " * 50
    metadata = {
        "_history_units": [unit, unit],
        "_history_unit_refs": [["old"], ["new"]],
        "_source_observation_refs": ["old"],
        "_dropped_context_ids": ["old"],
    }
    text = manager._fit_history_text(unit + "\n" + unit, manager.budget.count(unit) + 30, metadata)
    assert metadata["_observation_refs"] == ["new"]
    manager._record_context_manifest(metadata, {"history": text} if keep_history else {})
    manifest = metadata["context_manifest"]
    assert manifest["observation_refs"] == (["new"] if keep_history else [])
    assert set(manifest["selected_context_ids"]) == ({"old", "new"} if keep_history else {"old"})
    assert manifest["dropped_context_ids"] == ([] if keep_history else ["new"])


@pytest.mark.parametrize("valid_hash", [False, True])
def test_sealed_history_refs_require_matching_projection_then_include_new_tail(
    tmp_path, valid_hash
):
    agent = agent_at(tmp_path)
    agent.record({"role": "user", "content": "first"})
    init_run_projection(agent.session, "task")
    sealed = "sealed complete interaction"
    seal_history_at_build(agent.session, 1, sealed)
    agent.session["context_manifest"] = {
        "observation_refs": ["sealed-ref"],
        "section_hashes": {
            "history": hashlib.sha256(sealed.encode()).hexdigest() if valid_hash else "wrong"
        },
    }
    agent.record({"role": "user", "content": "new tail", "observation_id": "tail-ref"})
    manager, metadata = (ContextManager(agent), {})
    text = manager._get_compressed_history(metadata)
    manager._fit_history_text(text, 6000, metadata)
    assert metadata["_observation_refs"] == (
        ["sealed-ref", "tail-ref"] if valid_hash else ["tail-ref"]
    )
