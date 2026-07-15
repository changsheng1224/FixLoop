"""Strong step-level resume tests."""

from pathlib import Path

from agent_runtime.agent_loop import AgentLoop
from agent_runtime.cancellation import CancellationToken
from agent_runtime.checkpoint import evaluate_resume_state
from agent_runtime.config import AgentConfig
from agent_runtime.providers.clients import FakeModelClient
from agent_runtime.runtime import Agent
from agent_runtime.session_store import SessionStore
from agent_runtime.task_state import TaskState
from agent_runtime.workspace import WorkspaceContext


def _agent(root: Path, outputs: list[str]) -> Agent:
    config = AgentConfig(
        provider="fake",
        max_steps=5,
        max_new_tokens=256,
        approval="auto",
    )
    ws = WorkspaceContext.build(str(root))
    return Agent(
        config=config,
        model_client=FakeModelClient(outputs),
        workspace=ws,
        cwd=str(root),
    )


def test_successful_tool_step_is_persisted_as_resumable_checkpoint(temp_workspace):
    (temp_workspace / "README.md").write_text("hello\n", encoding="utf-8")
    agent = _agent(temp_workspace, [])
    loop = AgentLoop(agent)
    ts = TaskState.create(user_request="list files")
    loop._task_state = ts

    next_message = loop._run_tool_step(
        ts,
        "list_files",
        {"path": "."},
        step=1,
        path="xml",
    )

    saved = SessionStore(str(temp_workspace)).load(agent.session["id"])
    assert saved is not None
    checkpoint = saved["checkpoints"][-1]
    assert checkpoint["trigger"] == "step_end"
    assert checkpoint["resume_kind"] == "tool_step"
    assert checkpoint["step_index"] == 1
    assert checkpoint["path"] == "xml"
    assert checkpoint["tool"] == "list_files"
    assert checkpoint["tool_args"] == {"path": "."}
    assert checkpoint["tool_result"]
    assert checkpoint["next_user_message"] == next_message
    assert checkpoint["task_state"]["tool_steps"] == 1


def test_resume_continues_after_last_successful_tool_step(temp_workspace):
    (temp_workspace / "README.md").write_text("hello\n", encoding="utf-8")
    first = _agent(temp_workspace, [])
    loop = AgentLoop(first)
    ts = TaskState.create(user_request="list files")
    loop._task_state = ts
    loop._run_tool_step(ts, "list_files", {"path": "."}, step=1, path="xml")

    store = SessionStore(str(temp_workspace))
    restored = Agent.from_session(
        FakeModelClient(["<final>resume complete</final>"]),
        WorkspaceContext.build(str(temp_workspace)),
        store,
        first.session["id"],
        config=AgentConfig(
            provider="fake",
            max_steps=5,
            max_new_tokens=256,
            approval="auto",
        ),
        cwd=str(temp_workspace),
    )
    assert restored is not None
    restored.session["resume_state"] = evaluate_resume_state(restored)

    answer = restored.ask("ignored while step-resuming")

    assert "resume complete" in answer
    assert len(restored.model_client.prompts) == 1
    assert "工具 list_files 执行完成" in restored.model_client.prompts[0]


def test_write_step_resume_rejected_when_affected_file_changed(temp_workspace):
    target = temp_workspace / "app.py"
    target.write_text("x = 1\n", encoding="utf-8")
    agent = _agent(temp_workspace, [])
    loop = AgentLoop(agent)
    ts = TaskState.create(user_request="write file")
    loop._task_state = ts
    loop._run_tool_step(
        ts,
        "write_file",
        {"path": "app.py", "content": "x = 2\n"},
        step=1,
        path="xml",
    )

    target.write_text("x = 3\n", encoding="utf-8")
    restored = Agent.from_session(
        FakeModelClient(["<final>unused</final>"]),
        WorkspaceContext.build(str(temp_workspace)),
        SessionStore(str(temp_workspace)),
        agent.session["id"],
        config=AgentConfig(
            provider="fake",
            max_steps=5,
            max_new_tokens=256,
            approval="auto",
        ),
        cwd=str(temp_workspace),
    )
    assert restored is not None

    resume_state = evaluate_resume_state(restored)

    assert resume_state["status"] == "partial-stale"
    assert "app.py" in resume_state["stale_files"]


def test_step_checkpoint_not_persisted_if_cancelled_before_persist(temp_workspace):
    from agent_runtime.tool_executor import ToolExecutionResult

    agent = _agent(temp_workspace, [])
    token = CancellationToken()
    token.cancel("user")
    agent.cancel_token = token
    loop = AgentLoop(agent)
    ts = TaskState.create(user_request="list files")
    loop._task_state = ts

    loop._persist_step_checkpoint(
        ts,
        "list_files",
        {"path": "."},
        "README.md",
        "工具 list_files 执行完成。\n结果:\nREADME.md",
        ToolExecutionResult(
            content="README.md",
            metadata={"tool_status": "success"},
        ),
        step=1,
        path="xml",
    )

    assert SessionStore(str(temp_workspace)).load(agent.session["id"]) is None
    assert agent.session.get("checkpoints", []) == []


def test_user_cancel_checkpoint_records_in_flight_tool(temp_workspace):
    agent = _agent(temp_workspace, [])
    loop = AgentLoop(agent)
    ts = TaskState.create(user_request="write file")
    loop._task_state = ts

    answer = loop._finish_user_cancel(ts, phase="post_tool", in_flight="write_file")

    saved = SessionStore(str(temp_workspace)).load(agent.session["id"])
    assert "取消" in answer
    assert saved is not None
    checkpoint = saved["checkpoints"][-1]
    assert checkpoint["trigger"] == "user_cancel"
    assert checkpoint["in_flight_tool"] == "write_file"
    assert evaluate_resume_state(agent)["status"] != "step-resumable"
