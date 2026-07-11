"""CancellationToken 与 TaskState 用户取消单测。"""

import threading
import time

import pytest

from agent_runtime.cancellation import (
    CancelledError,
    CancellationToken,
    run_with_cancellation,
)
from agent_runtime.config import AgentConfig
from agent_runtime.providers.clients import FakeModelClient
from agent_runtime.runtime import Agent
from agent_runtime.stop_reasons import StopReason
from agent_runtime.task_state import TaskState
from agent_runtime.tool_executor import ToolExecutor


@pytest.fixture
def config():
    return AgentConfig(provider="fake", max_steps=4, approval="auto")


@pytest.fixture
def agent(config, workspace):
    client = FakeModelClient(["<final>ok</final>"])
    return Agent(config=config, model_client=client, workspace=workspace)


class TestCancellationToken:
    def test_cancel_sets_flag(self):
        token = CancellationToken()
        assert not token.is_cancelled
        token.cancel("user")
        assert token.is_cancelled
        assert token.reason == "user"

    def test_check_raises_when_cancelled(self):
        token = CancellationToken()
        token.cancel()
        with pytest.raises(CancelledError):
            token.check()


class TestRunWithCancellation:
    def test_aborts_when_already_cancelled(self):
        token = CancellationToken()
        token.cancel()
        with pytest.raises(CancelledError):
            run_with_cancellation(lambda: "x", token, poll_interval=0.01)

    def test_aborts_during_blocking_call(self):
        token = CancellationToken()

        def slow():
            time.sleep(1.0)
            return "done"

        def cancel_soon():
            time.sleep(0.05)
            token.cancel()

        threading.Thread(target=cancel_soon, daemon=True).start()
        with pytest.raises(CancelledError):
            run_with_cancellation(slow, token, poll_interval=0.02)


class TestReplCancel:
    def test_cancel_active_task(self):
        from agent_runtime.repl_cancel import (
            ReplCancelSession,
            cancel_active_repl_task,
            has_active_repl_task,
        )

        token = CancellationToken()
        session = ReplCancelSession(token)
        session.install()
        try:
            assert has_active_repl_task()
            assert cancel_active_repl_task()
            assert token.is_cancelled
        finally:
            session.restore()
        assert not has_active_repl_task()
        assert not cancel_active_repl_task()


class TestOllamaStreamCancel:
    def test_complete_stream_aborts_on_cancel(self, monkeypatch):
        from agent_runtime.providers.clients import OllamaModelClient

        token = CancellationToken()
        lines = [
            b'{"response":"a","done":false}\n',
            b'{"response":"b","done":false}\n',
        ]

        class FakeResp:
            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

            def __iter__(self):
                for line in lines:
                    token.cancel()
                    yield line

        monkeypatch.setattr("urllib.request.urlopen", lambda *args, **kwargs: FakeResp())
        client = OllamaModelClient()
        with pytest.raises(CancelledError):
            client.complete_stream("hello", cancel_token=token)


class TestTaskStateUserCancel:
    def test_stop_user_cancel(self):
        ts = TaskState.create(user_request="x")
        ts.stop_user_cancel(in_flight="write_file", phase="pre_tool")
        assert ts.stop_reason == StopReason.USER_CANCEL.value
        assert ts.status == "stopped"
        assert ts.node_timings["in_flight_tool"] == "write_file"
        assert ts.node_timings["cancel_phase"] == "pre_tool"


class TestAgentLoopCancel:
    def test_cancel_before_second_step(self, workspace):
        token = CancellationToken()

        class CancelAfterFirstTool(FakeModelClient):
            def complete(self, prompt, max_new_tokens=None):
                self._n = getattr(self, "_n", 0) + 1
                if self._n == 1:
                    token.cancel()
                return super().complete(prompt, max_new_tokens=max_new_tokens)

        config = AgentConfig(provider="fake", max_steps=5, approval="auto")
        agent = Agent(
            config=config,
            model_client=CancelAfterFirstTool(
                [
                    '<tool>{"name":"list_files","args":{"path":"."}}</tool>',
                    '<tool>{"name":"list_files","args":{"path":"."}}</tool>',
                    "<final>不应到达</final>",
                ]
            ),
            workspace=workspace,
        )
        agent.cancel_token = token
        answer = agent.ask("列出文件")
        assert "取消" in answer
        assert agent.cancel_token.is_cancelled


class TestToolExecutorCancel:
    def test_rejects_when_already_cancelled(self, agent):
        from agent_runtime.cancellation import CancellationToken

        agent.cancel_token = CancellationToken()
        agent.cancel_token.cancel()
        executor = ToolExecutor(agent=agent, approval_policy="auto")
        result = executor.execute("write_file", {"path": "x.txt", "content": "y"})
        assert result.metadata["tool_status"] == "rejected"
        assert result.metadata["tool_error_code"] == "cancelled"

    def test_restores_write_after_cancel(self, agent, temp_workspace, monkeypatch):
        from agent_runtime.cancellation import CancellationToken

        target = temp_workspace / "mut.py"
        target.write_text("before\n", encoding="utf-8")
        token = CancellationToken()
        agent.cancel_token = token
        executor = ToolExecutor(agent=agent, approval_policy="auto")
        original_run = agent.tools["write_file"]["run"]

        def write_then_cancel(args):
            token.cancel()
            return original_run(args)

        agent.tools["write_file"]["run"] = write_then_cancel
        result = executor.execute("write_file", {"path": "mut.py", "content": "after\n"})
        assert result.metadata["tool_status"] == "success"
        assert result.metadata.get("cancel_restored") is True
        assert target.read_text(encoding="utf-8") == "before\n"

    def test_restore_snapshot_roundtrip(self, agent, temp_workspace):
        (temp_workspace / "keep.py").write_text("v1\n", encoding="utf-8")
        executor = ToolExecutor(agent=agent, approval_policy="auto")
        snap = executor._capture_restore_snapshot()
        (temp_workspace / "keep.py").write_text("v2\n", encoding="utf-8")
        (temp_workspace / "new.py").write_text("new\n", encoding="utf-8")
        executor._restore_restore_snapshot(snap)
        assert (temp_workspace / "keep.py").read_text(encoding="utf-8") == "v1\n"
        assert not (temp_workspace / "new.py").exists()


class TestRepairCancel:
    def test_immediate_cancel_restores_repo(self, temp_workspace):
        from agent_runtime.cancellation import CancellationToken
        from agent_runtime.workspace import WorkspaceContext
        from src.agents.localizer import create_localizer
        from src.agents.patcher import create_patcher
        from src.agents.retriever import create_retriever
        from src.orchestrator import Orchestrator
        from src.repair.termination import RepairTerminalStatus

        ws = WorkspaceContext.build(str(temp_workspace))
        loc = FakeModelClient(['<final>[{"file_path":"app.py","start_line":1,"end_line":1,"reason":"x"}]</final>'])
        ret = FakeModelClient(['<final>{"related_tests":[]}</final>'])
        pat = FakeModelClient(['<final>[]</final>'])
        orch = Orchestrator(
            create_localizer(loc, ws),
            create_retriever(ret, ws),
            create_patcher(pat, ws),
            use_pytest_verify=False,
        )
        (temp_workspace / "app.py").write_text("x = 1\n", encoding="utf-8")
        token = CancellationToken()
        token.cancel()
        state = orch.repair("TypeError at app.py:1", cancel_token=token, repair_timeout_s=0)
        assert state.status == RepairTerminalStatus.USER_CANCEL
        assert state.node_timings.get("user_cancel")
        assert (temp_workspace / "app.py").read_text(encoding="utf-8") == "x = 1\n"
