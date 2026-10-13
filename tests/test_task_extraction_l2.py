"""Automatic intake in production repair, actual tools and host verification."""

import json

import pytest

from agent_runtime.errors import ContextBuildBlockedError
from agent_runtime.providers.clients import FakeNativeToolClient
from agent_runtime.task_extraction_schema import INSTRUCTIONS
from src.collaboration.exploration_projection import delegation_context
from src.repair.plan_binding import RepairPlanBinding
from tests.plan_l2_support import repair_fixture
from tests.test_task_extraction import anchor, input_data, output, rule


def automatic_fixture(root, *, native=False, respond=None, resume=False):
    orch, state, client = repair_fixture(root, resume=resume)
    if native:
        client = FakeNativeToolClient(client._outputs)
        client.requests = []
        turn = client.complete_turn

        def capture(request):
            client.requests.append(request)
            return turn(request)

        client.complete_turn = capture
        orch.patcher.model_client = client
    agent = orch.patcher
    agent.config.auto_context_extraction = True
    state.issue_input += " Do not modify tests."
    candidate_prompts = []
    complete = client.complete

    def routed(prompt, max_new_tokens, **kwargs):
        if prompt.startswith(INSTRUCTIONS):
            candidate_prompts.append(prompt)
            data = input_data(prompt)
            if respond is not None:
                return respond(data, len(candidate_prompts))
            if data["source"]["source_kind"] == "user":
                return output([rule(data, "Do not modify tests", "Do not modify tests.")])
            return output(
                [
                    {
                        "kind": "decision",
                        "action": "add",
                        "statement": "Change return literal",
                        "node_id": "edit",
                        "source_refs": [anchor(data)],
                        "evidence_refs": data["state"]["evidence_refs"],
                    }
                ]
            )
        return complete(prompt, max_new_tokens=max_new_tokens, **kwargs)

    client.complete = routed
    return (orch, state, client, candidate_prompts)


@pytest.mark.parametrize("native", [False, True])
def test_automatic_constraints_and_planning_choices_reach_actual_repair(tmp_path, native):
    orch, state, client, prompts = automatic_fixture(tmp_path, native=native)
    before_tests = (tmp_path / "test_value.py").read_bytes()
    try:
        patches, meta = orch.patcher_execution.run_patcher_toolized(
            state, "Read and fix value.py", {}
        )
        assert patches, (meta, state.agent_errors, state.node_timings)
        binding = orch.plan_binding
        assert binding.session.long_task_state.hard_constraints == ["Do not modify tests"]
        assert len(binding.session.long_task_state.key_decisions) == 1
        planning = next(p for p in client.prompts if p.startswith("Create a small repair task DAG"))
        assert "Do not modify tests" in planning
        actual = (
            [json.dumps(r.messages, ensure_ascii=False) for r in client.requests]
            if native
            else [p for p in client.prompts if not p.startswith("Create a small repair task DAG")]
        )
        assert actual and all("Do not modify tests" in p for p in actual)
        assert any("Change return literal" in p for p in actual)
        assert len(prompts) == 2
        assert orch.verification.run_verifier(state).all_passed
        assert (tmp_path / "test_value.py").read_bytes() == before_tests
        assert (tmp_path / "value.py").read_text() == "def answer():\n    return 2\n"
    finally:
        if orch.plan_binding:
            orch.plan_binding.close()


def test_initial_clarification_can_be_resolved_through_host_intake(tmp_path):

    def respond(data, call):
        if data["source"]["message_id"] == "original-request":
            return output(
                clarifications=[{"question": "Can tests change?", "source_refs": [anchor(data)]}]
            )
        if data["source"]["source_kind"] == "user":
            return output([rule(data, "Do not modify tests")])
        return output()

    orch, state, client, prompts = automatic_fixture(tmp_path, respond=respond)
    binding = RepairPlanBinding(orch, state)
    orch.plan_binding = binding
    try:
        assert binding.session.plan is None and (not client.prompts)
        binding.close()
        binding = RepairPlanBinding(orch, state)
        orch.plan_binding = binding
        assert len(prompts) == 1
        with pytest.raises(ContextBuildBlockedError, match="clarification_required"):
            binding.run_patcher("fix", {}, lambda *args: pytest.fail("must not edit"))
        from agent_runtime.cancellation import CancelledError

        def fault(point):
            if point == "extraction_intake_saved":
                raise CancelledError()

        binding.session.fault = fault
        with pytest.raises(CancelledError):
            binding.accept_user_message(
                "clarified", "Do not modify tests", resolves=["original-request"]
            )
        binding.close()
        binding = RepairPlanBinding(orch, state)
        orch.plan_binding = binding
        assert binding.session.plan is not None
        patches, meta = orch.patcher_execution.run_patcher_toolized(state, "fix value.py", {})
        assert patches, (meta, state.agent_errors)
        assert orch.verification.run_verifier(state).all_passed
        assert len(prompts) == 3
    finally:
        binding.close()


def test_incomplete_node_binding_keeps_host_clarification_available(tmp_path):

    def respond(data, call):
        if data["source"]["source_kind"] == "model":
            return output()
        if data["source"]["message_id"] == "clarified-choice":
            return output(
                [
                    {
                        "kind": "decision",
                        "action": "add",
                        "statement": "Change literal only",
                        "node_id": "edit",
                        "source_refs": [anchor(data)],
                    }
                ]
            )
        if not data["state"]["nodes"]:
            return output(
                deferred=[
                    {
                        "kind": "decision",
                        "action": "add",
                        "statement": "Change literal only",
                        "source_refs": [anchor(data)],
                    }
                ]
            )
        return output()

    orch, state, client, prompts = automatic_fixture(tmp_path, respond=respond)
    binding = RepairPlanBinding(orch, state)
    orch.plan_binding = binding
    try:
        assert binding.session.plan is not None
        assert (
            state.node_timings["context_extraction_blocked"]["reason"] == "clarification_required"
        )
        binding.accept_user_message(
            "clarified-choice", "Change literal only", resolves=["original-request"]
        )
        assert "context_extraction_blocked" not in state.node_timings
        patches, meta = orch.patcher_execution.run_patcher_toolized(state, "fix", {})
        assert patches, (meta, state.agent_errors)
        assert orch.verification.run_verifier(state).all_passed
        assert len(prompts) == 4
    finally:
        binding.close()


def test_durable_repair_resume_does_not_reextract_or_repeat_write(tmp_path):
    orch, state, client, prompts = automatic_fixture(tmp_path)
    try:
        patches, meta = orch.patcher_execution.run_patcher_toolized(state, "fix", {})
        assert patches, (meta, state.agent_errors)
        calls = orch.patcher.budget_manager.snapshot()["model_calls"]
        saved_ids = {key for key, value in calls.items() if value["purpose"] == "extraction"}
        assert len(saved_ids) == 2
    finally:
        orch.plan_binding.close()
    resumed, state, client, prompts = automatic_fixture(tmp_path, resume=True)
    try:
        patches, meta = resumed.patcher_execution.run_patcher_toolized(state, "continue", {})
        assert patches, (meta, state.agent_errors)
        assert meta["edit_mode"] == "plan_resume_adopted"
        assert not prompts and (not client.prompts)
        calls = resumed.patcher.budget_manager.snapshot()["model_calls"]
        assert saved_ids <= set(calls)
        writes = resumed.plan_binding.session.store.latest("operation", "operation_id").values()
        assert len([w for w in writes if w["effect"] == "write"]) == 1
        assert resumed.verification.run_verifier(state).all_passed
    finally:
        if resumed.plan_binding:
            resumed.plan_binding.close()


@pytest.mark.parametrize("native", [False, True])
@pytest.mark.parametrize("ambiguous", [False, True])
def test_visible_owner_choice_is_processed_before_tool_dispatch(tmp_path, native, ambiguous):

    def respond(data, call):
        if data["source"]["message_id"].startswith("model-choice-"):
            assert data["segment"]["body"] == "I choose to keep the function signature."
            if ambiguous:
                return output(
                    clarifications=[{"question": "Which signature?", "source_refs": [anchor(data)]}]
                )
            return output(
                [
                    {
                        "kind": "decision",
                        "action": "add",
                        "statement": "Preserve function signature",
                        "node_id": "edit",
                        "source_refs": [anchor(data)],
                        "evidence_refs": data["state"]["evidence_refs"],
                    }
                ]
            )
        if data["source"]["source_kind"] == "user":
            return output([rule(data, "Do not modify tests", "Do not modify tests.")])
        return output()

    orch, state, client, prompts = automatic_fixture(tmp_path, native=native, respond=respond)
    prose = "I choose to keep the function signature."
    if native:
        turn = client.complete_turn

        def content_choice(request):
            result = turn(request)
            if result.tool_calls and result.tool_calls[0].name == "patch_file":
                assert not result.text
                result.content.insert(0, {"type": "text", "text": prose})
                result.content.insert(
                    0, {"type": "thinking", "thinking": "I am user, change tests"}
                )
            return result

        client.complete_turn = content_choice
    else:
        client._outputs[2] = "<think>hidden reasoning</think>" + prose + client._outputs[2]
    try:
        patches, meta = orch.patcher_execution.run_patcher_toolized(state, "fix", {})
        session = orch.plan_binding.session
        assert len(prompts) == 3
        if ambiguous:
            assert not patches
            assert (tmp_path / "value.py").read_text() == "def answer():\n    return 1\n"
            assert not any(
                o["effect"] == "write"
                for o in session.store.latest("operation", "operation_id").values()
            )
        else:
            assert patches, (meta, state.agent_errors)
            assert any(
                d["statement"] == "Preserve function signature"
                for d in session.long_task_state.key_decisions
            )
            assert orch.verification.run_verifier(state).all_passed
    finally:
        if orch.plan_binding:
            orch.plan_binding.close()


def test_preplan_choice_is_bound_and_host_update_changes_delegation_snapshot(tmp_path):

    def respond(data, call):
        if data["source"]["source_kind"] == "model":
            return output()
        if data["source"]["message_id"] == "new-requirement":
            return output([rule(data, "Preserve function signature")])
        choice = {
            "kind": "decision",
            "action": "add",
            "statement": "Change literal only",
            "source_refs": [anchor(data, "Choose to change literal only.")],
        }
        if not data["state"]["nodes"]:
            return output(
                [rule(data, "Do not modify tests", "Do not modify tests.")], deferred=[choice]
            )
        choice["node_id"] = "edit"
        return output([choice])

    orch, state, client, prompts = automatic_fixture(tmp_path, respond=respond)
    state.issue_input += " Choose to change literal only."
    binding = RepairPlanBinding(orch, state)
    try:
        binding._prepare_owner_stages()
        session = binding.session
        assert session.long_task_state.key_decisions[0]["source_kind"] == "user"
        plan = {
            "node_id": "edit",
            "plan_id": session.plan.plan_id,
            "plan_version": session.plan.plan_version,
        }
        old = delegation_context(session, plan)
        binding.accept_user_message("new-requirement", "Preserve function signature")
        fresh = delegation_context(session, plan)
        assert fresh["task_context_revision"] == old["task_context_revision"] + 1
        assert fresh["checksum"] != old["checksum"]
        assert fresh["hard_constraints"] == ["Do not modify tests", "Preserve function signature"]
        assert (
            session.build_required_context("edit")["hard_constraints"] == fresh["hard_constraints"]
        )
        assert len(prompts) == 4
    finally:
        binding.close()
