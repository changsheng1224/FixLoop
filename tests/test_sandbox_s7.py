"""S7 main-flow contracts for task-bound sandbox execution."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from agent_runtime.linux_sandbox.task_context import (
    SandboxEnvironmentIdentity,
    SandboxTaskSpec,
)
from agent_runtime.tool_context import ToolContext
from src.orchestrator import Orchestrator
from src.state import RepairState


def _spec(tmp_path):
    return SandboxTaskSpec(
        SandboxEnvironmentIdentity("FixLoop-Test", "26.04", "runtime", "a" * 64),
        "wsl-bwrap-v1",
        "b" * 64,
        str(tmp_path.parent / (tmp_path.name + "-state")),
    )


class RecordingBackend:
    def __init__(self, binding, events=None, *, failure=None):
        self.task_binding = binding
        self.events = events if events is not None else []
        self.failure = failure
        self._closed = False

    def close(self):
        self.events.append("backend.close")
        if self.failure is not None:
            raise self.failure
        self._closed = True


class BindingAgent:
    def __init__(self, root, *, fail=False):
        self.tool_context = ToolContext(root=str(root), sandbox_mode=True)
        self._cwd = str(root)
        self.state_root = ""
        self.fail = fail
        self.bound = None

    def bind_sandbox_task(self, binding, backend):
        if self.fail:
            raise RuntimeError("agent binding failed")
        self.bound = (binding, backend)
        self.tool_context.root = binding.mapping.host_root
        binding.attach(self.tool_context)
        self.tool_context.sandbox_backend = backend
        self._cwd = binding.mapping.host_root
        self.state_root = binding.state_root


def _orchestrator(tmp_path, backend_factory, *, fail_agent=False):
    agent = BindingAgent(tmp_path, fail=fail_agent)
    spec = _spec(tmp_path)
    orch = Orchestrator(
        agent,
        sandbox_context=agent.tool_context,
        sandbox_task_spec=spec,
        sandbox_backend_factory=backend_factory,
    )
    orch._sandbox_repository_root = str(tmp_path)
    return orch, agent, spec


def test_task_binding_is_created_before_backend_and_shared_with_agent(tmp_path):
    created = []

    def factory(binding):
        backend = RecordingBackend(binding)
        created.append((binding, backend))
        return backend

    orch, agent, spec = _orchestrator(tmp_path, factory)
    state = RepairState(issue_input="fix", repair_run_id="repair-task")

    orch._prepare_sandbox_task(state, resume=False)

    binding, backend = created[0]
    assert agent.bound == (binding, backend)
    assert agent.tool_context.sandbox_task_binding == binding
    assert agent.tool_context.sandbox_backend is backend
    assert backend.task_binding == binding
    assert state.node_timings["sandbox_task_binding"] == binding.to_wire()
    assert Path(binding.state_root, "sandbox-task.json").exists()
    assert binding.environment == spec.environment


def test_repair_entry_binds_task_before_pipeline_and_closes_backend(tmp_path):
    events = []
    orch, agent, _specification = _orchestrator(
        tmp_path, lambda binding: RecordingBackend(binding, events)
    )
    orch._snapshot_repo = lambda: {}
    observed = []

    def fake_pipeline(state, _initial_snapshot):
        observed.append(
            (
                state.repair_run_id,
                agent.tool_context.sandbox_task_binding,
                agent.tool_context.sandbox_backend,
            )
        )
        return state

    orch._repair_impl_with_plan = fake_pipeline
    notified = []

    def ready(root):
        assert agent.tool_context.sandbox_task_binding.mapping.host_root == root
        assert not observed
        notified.append(root)

    result = orch.repair("fix", repair_timeout_s=0, run_id="repair-entry", workspace_ready=ready)

    assert result.repair_run_id == "repair-entry"
    assert observed and observed[0][0] == "repair-entry"
    assert observed[0][1] is not None
    assert observed[0][2] is not None
    assert events == ["backend.close"]
    assert notified == [str(tmp_path.resolve())]


def test_binding_failure_closes_pinned_backend_without_host_fallback(tmp_path):
    created = []

    def factory(binding):
        backend = RecordingBackend(binding)
        created.append(backend)
        return backend

    orch, _agent, _specification = _orchestrator(tmp_path, factory, fail_agent=True)
    state = RepairState(issue_input="fix", repair_run_id="repair-task")

    with pytest.raises(RuntimeError, match="agent binding failed"):
        orch._prepare_sandbox_task(state, resume=False)

    assert created[0].events == ["backend.close"]
    assert created[0]._closed is True
    assert orch._sandbox_context.sandbox_backend is None


def test_backend_binding_mismatch_closes_rejected_backend(tmp_path):
    created = []

    def factory(binding):
        backend = RecordingBackend(SimpleNamespace(), [])
        created.append(backend)
        return backend

    orch, _agent, _specification = _orchestrator(tmp_path, factory)
    state = RepairState(issue_input="fix", repair_run_id="repair-task")

    with pytest.raises(ValueError, match="sandbox_backend_binding_mismatch"):
        orch._prepare_sandbox_task(state, resume=False)

    assert created[0].events == ["backend.close"]


def test_uncaught_repair_exception_closes_backend_before_plan_binding(tmp_path):
    events = []
    orch, _agent, _specification = _orchestrator(
        tmp_path, lambda binding: RecordingBackend(binding, events)
    )
    backend = RecordingBackend(SimpleNamespace(), events)
    orch._sandbox_context.sandbox_backend = backend

    class Binding:
        def close(self):
            events.append("binding.close")

    orch._plan_binding = Binding()
    from unittest.mock import Mock

    runtime = Mock()
    orch._collaboration_runtime = runtime
    state = RepairState(issue_input="fix")
    orch._repair_impl_with_plan = lambda *_args: (_ for _ in ()).throw(RuntimeError("boom"))

    with pytest.raises(RuntimeError, match="boom"):
        orch._repair_impl(state)

    assert events == ["backend.close", "binding.close"]
    runtime.close.assert_called_once()


def test_uncertain_backend_cleanup_preserves_recovery_status(tmp_path):
    events = []
    orch, _agent, _specification = _orchestrator(
        tmp_path,
        lambda binding: RecordingBackend(binding, events),
    )
    backend = RecordingBackend(SimpleNamespace(), events, failure=ValueError("execution_uncertain"))
    orch._sandbox_context.sandbox_backend = backend

    class Coordinator:
        lease = object()

        def finish(self, status, *, error_code):
            events.append(("coordinator.finish", status, error_code))
            return SimpleNamespace(status=status)

    class Binding:
        coordinator = Coordinator()

        def close(self):
            events.append("binding.close")

    orch._plan_binding = Binding()
    state = RepairState(issue_input="fix")
    orch._repair_impl_with_plan = lambda *_args: (_ for _ in ()).throw(RuntimeError("boom"))

    with pytest.raises(RuntimeError, match="boom"):
        orch._repair_impl(state)

    assert state.status == "recovery_required"
    assert state.control.coordination_status == "recovery_required"
    assert events == [
        "backend.close",
        ("coordinator.finish", "recovery_required", "sandbox_cleanup_unconfirmed"),
        "binding.close",
    ]


def test_invalid_resume_checkpoint_closes_backend_before_return(tmp_path):
    from agent_runtime.linux_sandbox.task_context import SandboxTaskBinding

    events = []
    orch, _agent, spec = _orchestrator(
        tmp_path, lambda binding: RecordingBackend(binding, events)
    )
    binding = SandboxTaskBinding.create(tmp_path, tmp_path, "repair-task", spec)
    binding.save()
    orch._snapshot_repo = lambda: {}

    result = orch.repair("fix", resume_run_id="repair-task")

    assert result.status == "recovery_required"
    assert result.agent_errors["checkpoint"]
    assert events == ["backend.close"]


def test_cancel_before_pipeline_closes_backend(tmp_path):
    from agent_runtime.cancellation import CancellationToken

    events = []
    orch, _agent, _specification = _orchestrator(
        tmp_path, lambda binding: RecordingBackend(binding, events)
    )
    orch._snapshot_repo = lambda: {}
    token = CancellationToken()
    token.cancel("user")

    result = orch.repair("fix", cancel_token=token, run_id="repair-cancel")

    assert result.status == "user_cancel"
    assert events == ["backend.close"]
