"""Docker execution evidence must not claim completion after uncertain cleanup."""

import json
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from src.harness.python_runner import PythonTestRunner
from src.harness.sandbox_manager import ExecResult, Sandbox, SandboxManager
from src.harness.sandbox_verify import run_sandbox_verification_flow


class TestManager:
    __test__ = False

    def __init__(self, code=0, *, removed=True):
        self.code = code
        self.removed = removed
        self.commands = []
        self.destroyed = []

    def create(self, repo):
        return Sandbox(id="test-container", profile="python")

    def execute(self, sandbox, command, **kwargs):
        self.commands.append(command)
        if command.startswith("cat "):
            summary = {"total": 1, "passed": int(self.code == 0), "failed": int(self.code == 1)}
            return ExecResult(0, json.dumps({"summary": summary}), "")
        return ExecResult(self.code, "pytest output", "", cancelled=self.code == -2)

    def destroy(self, sandbox):
        self.destroyed.append(sandbox.id)
        return self.removed


@pytest.mark.parametrize("code,completed", [(0, True), (1, True), (-1, False), (-2, False)])
def test_runner_records_actual_execution_and_interruption(code, completed):
    manager = TestManager(code)
    runner = PythonTestRunner(manager)
    result = runner.run(Sandbox("test-container", "python"), "test_value.py::test_answer")
    assert result.all_passed is (code == 0)
    assert runner.execution_receipt["completed"] is completed
    assert runner.execution_receipt["pytest_exit_code"] == code
    assert runner.execution_receipt["sandbox_id"] == "test-container"
    assert "test_value.py::test_answer" in runner.execution_receipt["command"][-1]
    assert runner.execution_receipt["receipt_id"]


@pytest.mark.parametrize("removed", [True, False])
def test_flow_requires_confirmed_removal_for_completion(tmp_path, monkeypatch, removed):
    manager = TestManager(removed=removed)
    monkeypatch.setattr("src.harness.sandbox_verify.assert_sandbox_available", lambda: None)
    monkeypatch.setattr("src.harness.sandbox_verify.SandboxManager", lambda: manager)
    context = SimpleNamespace()
    result, evidence = run_sandbox_verification_flow(context, str(tmp_path), "test_value.py")
    assert result.all_passed
    assert manager.destroyed == ["test-container"]
    assert evidence["completed"] is removed
    assert evidence["cleanup"] == ("confirmed" if removed else "unconfirmed")
    assert evidence["pytest_exit_code"] == 0


@pytest.mark.parametrize(
    "kill_error,remove_error,expected",
    [(False, False, True), (True, False, True), (False, True, False)],
)
def test_destroy_confirms_force_removal_even_when_kill_fails(kill_error, remove_error, expected):
    pytest.importorskip("docker", reason="requires the optional sandbox SDK, not a Docker daemon")
    manager = SandboxManager()
    manager._docker = MagicMock()
    container = manager._docker.containers.get.return_value
    if kill_error:
        container.kill.side_effect = RuntimeError("already stopped")
    if remove_error:
        container.remove.side_effect = RuntimeError("daemon unavailable")
    assert manager.destroy(Sandbox("test-container", "python")) is expected
    container.remove.assert_called_once_with(force=True)


def test_destroy_confirms_absence_after_auto_remove_conflict():
    pytest.importorskip("docker", reason="requires the optional sandbox SDK, not a Docker daemon")
    from docker.errors import APIError, NotFound

    manager = SandboxManager()
    manager._docker = MagicMock()
    container = MagicMock()
    manager._docker.containers.get.side_effect = [container, NotFound("removed")]
    container.remove.side_effect = APIError(
        "removal in progress", response=SimpleNamespace(status_code=409)
    )
    assert manager.destroy(Sandbox("test-container", "python")) is True


def test_destroy_does_not_confirm_conflict_without_observed_absence(monkeypatch):
    pytest.importorskip("docker", reason="requires the optional sandbox SDK, not a Docker daemon")
    from docker.errors import APIError

    monkeypatch.setattr("src.harness.sandbox_manager.time.sleep", lambda _: None)
    manager = SandboxManager()
    manager._docker = MagicMock()
    manager._docker.containers.get.return_value.remove.side_effect = APIError(
        "removal in progress", response=SimpleNamespace(status_code=409)
    )
    assert manager.destroy(Sandbox("test-container", "python")) is False
