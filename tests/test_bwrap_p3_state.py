import pytest

from agent_runtime.checkpoint import create_checkpoint, evaluate_resume_state
from agent_runtime.context_runtime import ObservationStore
from agent_runtime.session_store import SessionStore
from agent_runtime.state_root import state_root_for
from agent_runtime.task_state import TaskState


def test_state_root_is_external(tmp_path):
    workspace = tmp_path / "workspace"
    state = tmp_path / "trusted-state"
    workspace.mkdir()
    resolved = state_root_for(workspace, state)
    assert resolved == state.resolve()
    store = SessionStore(str(workspace), state_root=str(state))
    store.save({"id": "s1", "history": []})
    assert (state / ".agent" / "sessions" / "s1.json").is_file()
    assert not (workspace / ".agent").exists()


def test_state_root_inside_workspace_rejected(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    with pytest.raises(ValueError, match="state_root inside workspace"):
        state_root_for(workspace, workspace / ".state")


def test_observations_use_external_root(tmp_path):
    workspace = tmp_path / "workspace"
    state = tmp_path / "state"
    workspace.mkdir()
    session = {"id": "s1", "session_scope": {"workspace_id": "w1"}}
    store = ObservationStore(session, str(workspace), str(state))
    record = store.put("read_file", {"path": "a.py"}, "secret output")
    store.close()
    assert record.raw_ref.startswith(("memory:", str(state)))
    assert not (workspace / ".agent").exists()


def test_sandbox_step_resume_is_uncertain(tmp_path):
    from types import SimpleNamespace

    class Prefix:
        tool_signature = "tools"
        assets_fingerprint = "assets"

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    session = {
        "id": "s1",
        "session_scope": {"session_id": "s1", "workspace_id": "w1"},
        "memory": {"working": {"recent_files": []}},
        "checkpoints": [],
        "checkpoint_sequence": 0,
    }
    agent = SimpleNamespace(
        _cwd=str(workspace),
        config=SimpleNamespace(provider="fake", model="m", approval="auto", max_steps=3),
        _prefix=Prefix(),
        session=session,
        tool_context=SimpleNamespace(
            sandbox_identity={"backend": "wsl_bwrap", "policy_digest": "p1"}
        ),
        _loop=None,
    )
    ts = TaskState.create(user_request="run")
    create_checkpoint(
        agent,
        ts,
        "run",
        trigger="step_end",
        last_tool="run_shell",
        step_payload={
            "resume_kind": "tool_step",
            "tool": "run_shell",
            "next_user_message": "continue",
            "result_metadata": {"call_id": "call-1"},
        },
    )
    result = evaluate_resume_state(agent)
    assert result["status"] == "sandbox-step-uncertain"
    assert "tool_args" not in result["resume_observation"]
