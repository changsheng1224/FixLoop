"""Timeout and failure retain ownership until execution returns."""

from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from agent_runtime.cancellation import CancellationToken
from agent_runtime.config import AgentConfig
from agent_runtime.providers.clients import FakeModelClient
from agent_runtime.runtime import Agent
from agent_runtime.workspace import WorkspaceContext
from src.repair.phase_clock import PhaseTimeoutConfig
from src.repair.service import RepairRequest, RepairService
from src.repair.session import ExecutionSession


def test_primary_phase_budget_skips_localize():
    config = PhaseTimeoutConfig.for_patcher_primary(900)
    assert config.localize_s == 0
    assert config.patch_s > 0 and config.verify_s > 0
    assert config.patch_s + config.verify_s <= 900


def test_timeout_keeps_token_and_budget_until_execution_returns(tmp_path, monkeypatch):
    agent = Agent(
        AgentConfig(provider="fake"), FakeModelClient([]), WorkspaceContext.build(str(tmp_path))
    )
    session = ExecutionSession(agent, execution_tier="host")
    token = CancellationToken()
    timers = []

    class ImmediateTimer:
        """Stands in for ``threading.Timer``; runs the callback synchronously.

        It must offer the surface the caller uses, not just the two methods the
        happy path needs: the repair lifecycle joins the timer after cancelling
        it so it does not report a terminal state while the cancel callback is
        still running, and asks whether it survived the join.
        """

        def __init__(self, seconds, callback):
            self.callback, self.cancelled, self.finished = callback, False, False
            timers.append(self)

        def start(self):
            try:
                self.callback()
            finally:
                self.finished = True

        def cancel(self):
            self.cancelled = True

        def join(self, timeout=None):
            return None

        def is_alive(self):
            return not self.finished and not self.cancelled

    def execute(state, initial_snapshot):
        assert token.is_cancelled and token.reason == "timeout"
        assert agent.cancel_token is token
        assert session.repair_ctx.cancel_token is token
        assert agent.budget_manager is session.model_budget
        assert session.model_budget is not None
        state.set_status("user_cancel", "cooperative_stop")
        return state

    monkeypatch.setattr("src.repair.execution.lifecycle.threading.Timer", ImmediateTimer)
    monkeypatch.setattr(session.coordinator, "repair_impl", execute)
    result = RepairService(session).run(RepairRequest("fix", timeout_s=1, cancel_token=token))
    assert result.status == "timeout"
    assert timers[0].cancelled
    assert session.repair_ctx is session.model_budget is session.model_deadline is None
    assert agent.cancel_token is agent.budget_manager is None
    assert not session.run_lock.locked()


def test_cleanup_failure_still_releases_edit_lock_and_heartbeat():
    session = ExecutionSession(None)
    lock = object()
    session.edit_lock = lock
    session.patcher = SimpleNamespace(tool_context=SimpleNamespace(edit_lock=lock))
    session.collaboration_runtime = SimpleNamespace(close=Mock(side_effect=ValueError("cleanup")))
    session.progress = SimpleNamespace(stop_heartbeat=Mock())
    with pytest.raises(ValueError, match="cleanup"):
        session.coordinator.release_repair_resources()
    assert session.edit_lock is session.patcher.tool_context.edit_lock is None
    session.progress.stop_heartbeat.assert_called_once()
