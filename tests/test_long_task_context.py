from types import SimpleNamespace

import pytest

from agent_runtime.compression_pipeline import is_repair_state_item
from agent_runtime.context_manager import ContextManager
from agent_runtime.plan_runtime import LongTaskState
from tests.plan_support import read, session_for, simple_plan


def test_long_task_context_preserves_request_constraints_and_node(tmp_path):
    (tmp_path / "value.py").write_text("value = 1\n")
    with session_for(tmp_path) as session:
        session.create(simple_plan(session))
        session.configure_long_task("fix the value bug", hard_constraints=["do not edit tests"])
        context = session.build_long_task_context("read-0")
        assert context["original_request"] == "fix the value bug"
        assert context["hard_constraints"] == ["do not edit tests"]
        assert context["current_node"]["node_id"] == "read-0"


def test_long_task_context_marks_changed_evidence_stale(tmp_path):
    (tmp_path / "value.py").write_text("value = 1\n")
    with session_for(tmp_path) as session:
        session.create(simple_plan(session))
        session.run_node("read-0", lambda attempt: read(session, attempt))
        ref = session.plan.node("read-0").output_evidence_refs[0]
        session.long_task.add_evidence([ref])
        (tmp_path / "value.py").write_text("value = 2\n")
        context = session.build_long_task_context("read-0")
        assert ref in context["stale_evidence"]
        assert context["needs_evidence_refresh"] is True


def test_long_task_state_checkpoint_roundtrip_and_tamper_detection(tmp_path):
    state = LongTaskState("task", "run", original_request="request")
    sealed = state.seal()
    assert LongTaskState.verify(sealed).original_request == "request"
    with pytest.raises(ValueError, match="checksum"):
        LongTaskState.verify({**sealed, "original_request": "tampered"})


def test_plan_checkpoint_seals_and_verifies_long_task_state(tmp_path):
    (tmp_path / "value.py").write_text("value = 1\n")
    with session_for(tmp_path) as session:
        session.create(simple_plan(session))
        session.configure_long_task("request", hard_constraints=["constraint"])
        seal = session.checkpoint()
        assert seal["long_task_state"]["original_request"] == "request"
        session.verify_long_task_checkpoint(seal)
        with pytest.raises(ValueError, match="checkpoint_identity_or_checksum"):
            session.verify_long_task_checkpoint(
                {**seal, "long_task_state": {**seal["long_task_state"], "original_request": "tampered"}}
            )


def test_context_manager_projects_long_task_state_into_protected_section(tmp_path):
    (tmp_path / "value.py").write_text("value = 1\n")
    with session_for(tmp_path) as session:
        session.create(simple_plan(session))
        session.configure_long_task("request", hard_constraints=["constraint"])
        agent = SimpleNamespace(
            _plan_session=session,
            session={},
            _cwd=str(tmp_path),
            _prefix=SimpleNamespace(
                tool_signature="", assets_fingerprint="", stable_system_text="",
                stable_tools_text="", stable_skills_text="", stable_workspace_text="",
                role_text="",
                hash="", workspace_fingerprint="",
            ),
            tool_context=SimpleNamespace(),
            config=SimpleNamespace(prompt_budget=6000, model="", provider=""),
            read_history=lambda: [],
        )
        manager = ContextManager(agent, total_budget=6000)
        text, metadata = manager.build("request")
        assert "长任务状态" in text
        assert metadata["long_task_context"]["original_request"] == "request"
        assert agent.session["long_task_context"]["hard_constraints"] == ["constraint"]
        assert is_repair_state_item({"content": text.split("长任务状态", 1)[1], "long_task_state": True})


def test_refresh_observation_with_new_plan_node_records_replacement(tmp_path):
    (tmp_path / "value.py").write_text("value = 1\n")
    with session_for(tmp_path) as session:
        session.create(simple_plan(session, reads=2))
        session.run_node("read-0", lambda attempt: read(session, attempt))
        old_ref = session.plan.node("read-0").output_evidence_refs[0]
        session.long_task.add_evidence([old_ref])
        (tmp_path / "value.py").write_text("value = 2\n")
        new_ref = session.refresh_observation_with_node(old_ref, "read-1", lambda attempt: read(session, attempt))
        assert new_ref != old_ref
        assert session.evidence.valid(new_ref)
        assert any(item.get("supersedes") == old_ref for item in session.long_task_state.key_decisions)
