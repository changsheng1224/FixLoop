"""S5 authorization, sealed approvals and runtime pause boundaries."""

from copy import copy
from dataclasses import replace

import pytest

from agent_runtime.cancellation import CancellationToken
from agent_runtime.linux_sandbox.models import SandboxRequest, SandboxResult
from agent_runtime.linux_sandbox.permissions import (
    PermissionScope,
    PermissionService,
    permission_service,
    request_action,
)
from agent_runtime.linux_sandbox.workspace_routing import execute_workspace_tool
from agent_runtime.tool_context import ToolContext
from agent_runtime.tool_executor import ToolExecutor
from tests.test_sandbox_workspace_routing import DIGEST, Backend, make_agent


def request(**fields):
    return replace(
        SandboxRequest(
            "workspace",
            "task",
            "run",
            "call",
            "workspace",
            (),
            schema_version="2",
            tool_name="read_file",
            tool_args={"path": "src/a.py"},
        ),
        **fields,
    )


def installer(**fields):
    return replace(
        request(
            operation="command",
            schema_version="1",
            tool_name="",
            tool_args={},
            argv=("/toolchain/bin/python", "-m", "pip", "install", "deps/demo.whl"),
            output_limit_bytes=65536,
        ),
        **fields,
    )


@pytest.mark.parametrize(
    "req,capability",
    [
        (request(), "workspace_read"),
        (
            request(
                tool_name="write_file",
                tool_args={"path": "src/a.py", "content": "x"},
                base_versions={"src/a.py": DIGEST},
            ),
            "workspace_write",
        ),
        (
            request(
                operation="command",
                schema_version="1",
                tool_name="",
                tool_args={},
                argv=("/toolchain/bin/python", "-c", "print(1)"),
                output_limit_bytes=65536,
            ),
            "python",
        ),
        (
            request(
                operation="command",
                schema_version="1",
                tool_name="",
                tool_args={},
                argv=("/toolchain/bin/python", "-m", "pytest", "tests/test_a.py"),
                output_limit_bytes=65536,
            ),
            "pytest",
        ),
    ],
)
def test_existing_scope_automatically_authorizes_actual_operation(req, capability):
    service = PermissionService({"task": "task"})
    evidence = service.authorize(req, callback=lambda *_: pytest.fail("unnecessary approval"))
    assert evidence["status"] == "allowed"
    assert request_action(req)["capability"] == capability
    consumed = service.consume(req)
    service.assert_current(consumed)
    with pytest.raises(ValueError, match="authorization_required"):
        service.consume(req)
    with pytest.raises(ValueError, match="authorization_replayed"):
        service.authorize(req)


def test_path_scope_matches_components_not_string_prefixes():
    service = PermissionService({}, PermissionScope(read_paths=("src",)))
    assert service.authorize(request())["status"] == "allowed"
    assert (
        service.authorize(
            request(call_id="other", tool_args={"path": "src-secret/a.py"}), policy="auto"
        )["status"]
        == "approval_required"
    )
    assert service.snapshot()["scope"]["read_paths"] == ("src",)
    with pytest.raises(ValueError):
        PermissionScope(read_paths=("../secret",))


@pytest.mark.parametrize(
    "callback,status",
    [
        (None, "approval_required"),
        (lambda _: None, "approval_required"),
        (lambda _: False, "approval_denied"),
    ],
)
def test_scope_expansion_waits_or_denies_without_tickets(callback, status):
    service = PermissionService({})
    result = service.authorize(installer(), policy="auto", callback=callback)
    assert result["status"] == status
    assert not service.snapshot()["tickets"]
    assert "offline_install" not in service.snapshot()["scope"]["capabilities"]


def test_eof_keeps_pending_and_interrupt_cancels_it():
    service = PermissionService({})

    def eof(_):
        raise EOFError

    assert service.authorize(installer(), callback=eof)["status"] == "approval_required"
    service.cancel_pending()
    token = CancellationToken()
    other = PermissionService({})

    def interrupt(_):
        raise KeyboardInterrupt

    assert (
        other.authorize(installer(), callback=interrupt, cancel_token=token)["status"]
        == "cancelled"
    )
    assert token.is_cancelled
    assert other.snapshot()["pending"]["status"] == "stale"


def test_approved_scope_is_committed_only_after_exact_pinned_inputs_consumed():
    service = PermissionService({})
    req = installer(cwd_relative="sub")
    versions = {"sub/deps/demo.whl": DIGEST}
    decision = service.authorize(req, callback=lambda _: True, input_versions=versions)
    assert decision["status"] == "allowed"
    assert request_action(req)["installs"] == ["sub/deps/demo.whl"]
    assert "offline_install" not in service.snapshot()["scope"]["capabilities"]
    with pytest.raises(ValueError, match="authorization_required"):
        service.consume(req, input_versions={"sub/deps/demo.whl": "b" * 64})
    assert "offline_install" not in service.snapshot()["scope"]["capabilities"]
    evidence = service.consume(req, input_versions=versions)
    service.assert_current(evidence)
    assert service.snapshot()["pending"]["status"] == "consumed"
    assert (
        service.authorize(replace(req, call_id="reuse"), input_versions=versions)["status"]
        == "allowed"
    )
    assert service.authorize(installer(call_id="different"))["status"] == "approval_required"


@pytest.mark.parametrize(
    "changes",
    [
        {"timeout_s": 19},
        {"owner_token": "owner", "generation": 1},
        {"generation": 2},
        {"coordination_revision": 1},
        {"argv": ("/toolchain/bin/python", "-m", "pip", "install", "deps/other.whl")},
    ],
)
def test_approval_cannot_authorize_changed_invocation(changes):
    service = PermissionService({})
    req = installer()
    pending = service.authorize(req)
    service.resolve(pending["approval_id"], allow=True)
    assert (
        service.authorize(replace(req, call_id="changed", **changes))["status"]
        == "approval_pending"
    )
    assert not service.snapshot()["tickets"]


def test_revocation_invalidates_approved_proposal_and_consumed_evidence():
    service = PermissionService({})
    req = request()
    service.authorize(req)
    evidence = service.consume(req)
    service.replace_scope(PermissionScope(read_paths=()))
    with pytest.raises(ValueError, match="authorization_revoked"):
        service.assert_current(evidence)
    pending = service.authorize(replace(req, call_id="second"))
    service.resolve(pending["approval_id"], allow=True)
    service.replace_scope(PermissionScope(read_paths=()))
    assert service.authorize(replace(req, call_id="third"))["status"] == "approval_stale"


def test_durable_approval_restart_is_bound_to_external_identity(tmp_path):
    root, state = tmp_path / "repo", tmp_path / "external"
    root.mkdir()
    identity = {"workspace": "w", "task": "t", "run": "r"}
    first = PermissionService(identity, state_root=state, workspace=root)
    pending = first.authorize(installer())
    second = PermissionService(identity, state_root=state, workspace=root)
    second.resolve(pending["approval_id"], allow=True)
    second.authorize(installer(call_id="resumed"))
    second.consume(installer(call_id="resumed"))
    third = PermissionService(identity, state_root=state, workspace=root)
    assert "offline_install" in third.snapshot()["scope"]["capabilities"]
    assert third.snapshot()["pending"]["status"] == "consumed"
    with pytest.raises(ValueError, match="authorization_replayed"):
        third.authorize(installer(call_id="resumed"))
    other = PermissionService({**identity, "run": "different"}, state_root=state, workspace=root)
    assert other.authorize(installer())["status"] == "approval_required"


def test_restore_still_requires_runtime_ownership_and_current_preimages():
    service = PermissionService({})
    req = request(
        tool_name="workspace_restore",
        tool_args={"files": {"src/a.py": "original"}},
        base_versions={"src/a.py": DIGEST},
    )
    with pytest.raises(ValueError, match="runtime-owned"):
        service.authorize(req, restore_owned={})
    with pytest.raises(ValueError, match="stale_preimage"):
        service.authorize(
            req, restore_owned={"src/a.py": DIGEST}, input_versions={"src/a.py": "b" * 64}
        )


def test_no_general_shell_or_network_capability_can_be_granted():
    with pytest.raises(ValueError, match="unsupported capability"):
        PermissionScope(capabilities=("host_shell",))
    with pytest.raises(ValueError, match="policy_denied"):
        request_action(installer(argv=("/bin/sh", "-c", "echo unsafe")))


@pytest.fixture
def ctx(tmp_path):
    return ToolContext(str(tmp_path), sandbox_backend=Backend())


def test_shallow_contexts_share_authority_and_reject_injected_service(ctx):
    other = copy(ctx)
    assert permission_service(ctx) is permission_service(other)
    other.sandbox_permissions = PermissionService({})
    with pytest.raises(ValueError, match="binding_mismatch"):
        permission_service(other)


def test_unbound_context_rejects_permission_service_with_wrong_identity(ctx):
    ctx.sandbox_permissions = PermissionService({})
    with pytest.raises(ValueError, match="binding_mismatch"):
        permission_service(ctx)


def test_resource_registration_failure_discards_authorization_ticket(ctx):
    service = permission_service(ctx)
    ctx.run_coordinator = type(
        "Coordinator",
        (),
        {
            "lease": type(
                "Lease",
                (),
                {
                    "run_id": "run",
                    "task_id": "task",
                    "owner_token": "owner",
                    "generation": 1,
                    "coordination_revision": 1,
                },
            )(),
            "assert_can_dispatch": lambda self: None,
            "register_resource": lambda self, **_: (_ for _ in ()).throw(
                ValueError("coordination unavailable")
            ),
        },
    )()
    result = execute_workspace_tool(ctx, "read_file", {"path": "a.py"})
    assert result.error_code == "coordination unavailable"
    assert service.snapshot()["tickets"] == {}
    assert any(item["event"] == "not_started" for item in service.snapshot()["audit"])


def test_native_backend_reply_must_include_consumed_permission(ctx):
    ctx.sandbox_backend.requires_authorization = True
    ctx.sandbox_backend.handler = lambda request: SandboxResult(
        "completed",
        exit_code=0,
        cleanup="confirmed",
        actual_backend="linux_sandbox",
        receipt_id=request.call_id,
    )
    result = execute_workspace_tool(ctx, "read_file", {"path": "a.py"})
    assert result.status == "uncertain"
    assert result.error_code == "authorization_unconfirmed"


@pytest.mark.parametrize(
    "name,args",
    [
        ("stack_parse", {"traceback": "Traceback (most recent call last):"}),
        ("read_file", {"path": "a.py"}),
        ("write_file", {"path": "a.py", "content": "pass\n"}),
    ],
)
def test_registry_mutation_cannot_inject_host_execution_or_snapshots(
    ctx, tmp_path, monkeypatch, name, args
):
    agent = make_agent(ctx, tmp_path)
    ctx.sandbox_workspace_versions["a.py"] = DIGEST
    if name == "stack_parse":
        from src.tools.composite import build_repair_canonical_tools

        agent.tools[name] = build_repair_canonical_tools(ctx)[name]
        agent._tool_names = (*agent._tool_names, name)
    spec = agent.tools[name]
    spec.update(
        run=lambda _: pytest.fail("injected host handler"),
        execution_mode="process",
        execution_tier="host",
        terminal=True,
        isolated_read=True,
    )
    executor = ToolExecutor(agent=agent, approval_policy="auto")
    if name != "stack_parse":
        monkeypatch.setattr(executor, "_capture_snapshot", lambda: pytest.fail("host snapshot"))
    result = executor.execute_gated(name, args)
    assert result.error_code != "runtime_error"
    assert result.metadata["execution_tier"] == (
        "trusted_host" if name == "stack_parse" else "linux_sandbox"
    )


def test_pending_edit_has_no_backend_call_or_effect(ctx):
    ctx.sandbox_initial_scope = PermissionScope(write_paths=())
    ctx.sandbox_workspace_versions["a.py"] = DIGEST
    result = execute_workspace_tool(ctx, "write_file", {"path": "a.py", "content": "new"})
    assert result.error_code == "approval_required"
    assert result.metadata["sandbox_mutation_status"] == "not_started"
    assert not ctx.sandbox_backend.calls
    assert not ctx.sandbox_uncertain_event.is_set()


@pytest.mark.parametrize("allow", [True, False])
def test_agent_pauses_and_resumes_original_call_without_model_replanning(ctx, tmp_path, allow):
    from agent_runtime.agent_loop import AgentLoop
    from agent_runtime.checkpoint import evaluate_resume_state
    from agent_runtime.providers.clients import FakeModelClient
    from agent_runtime.task_state import TaskState

    ctx.sandbox_initial_scope = PermissionScope(write_paths=())
    agent = make_agent(ctx, tmp_path)
    agent.model_client = FakeModelClient(["<final>continued</final>"])
    ctx.sandbox_workspace_versions["a.py"] = DIGEST
    loop = AgentLoop(agent)
    ts = TaskState.create(user_request="edit")
    ts.advance_runtime("reasoning")
    loop._task_state = ts
    loop._run_tool_step(ts, "write_file", {"path": "a.py", "content": "new"}, step=1, path="xml")
    assert ts.stop_reason == "approval_required"
    assert evaluate_resume_state(agent)["status"] == "approval-pending"
    assert not any(r.tool_name == "write_file" for r in ctx.sandbox_backend.calls)
    pending = agent.pending_sandbox_approval()
    agent.resolve_sandbox_approval(pending["id"], allow=allow)
    assert evaluate_resume_state(agent)["status"] == "approval-resumable"
    assert "continued" in agent.ask("ignored")
    writes = [r for r in ctx.sandbox_backend.calls if r.tool_name == "write_file"]
    assert len(writes) == int(allow)
    if allow:
        assert writes[0].tool_args == {"path": "a.py", "content": "new"}


def test_agent_approval_checkpoint_resumes_after_coordinator_takeover(ctx, tmp_path):
    from agent_runtime.agent_loop import AgentLoop
    from agent_runtime.checkpoint import evaluate_resume_state
    from agent_runtime.run_coordination.coordinator import RunCoordinator
    from agent_runtime.task_state import TaskState

    state_root = tmp_path.parent / (tmp_path.name + "-state")
    old = RunCoordinator(ctx.root, "task", "run", state_root=str(state_root))
    old.acquire()
    old.reconcile()
    ctx.run_coordinator = old
    ctx.sandbox_workspace_id = old.lease.workspace_id
    ctx.sandbox_initial_scope = PermissionScope(write_paths=())
    ctx.sandbox_workspace_versions["a.py"] = DIGEST
    agent = make_agent(ctx, tmp_path)
    loop = AgentLoop(agent)
    ts = TaskState.create(user_request="edit")
    ts.advance_runtime("reasoning")
    loop._task_state = ts
    loop._run_tool_step(ts, "write_file", {"path": "a.py", "content": "new"}, step=1, path="xml")
    assert evaluate_resume_state(agent)["status"] == "approval-pending"
    pending = agent.pending_sandbox_approval()
    agent.resolve_sandbox_approval(pending["id"], allow=True)
    old.finish("released")

    new = RunCoordinator(ctx.root, "task", "run", state_root=str(state_root))
    new.acquire()
    new.reconcile()
    ctx.run_coordinator = new
    try:
        assert evaluate_resume_state(agent)["status"] == "approval-resumable"
        assert "done" in agent.ask("continue")
        writes = [r for r in ctx.sandbox_backend.calls if r.tool_name == "write_file"]
        assert len(writes) == 1
        assert writes[0].tool_args == {"path": "a.py", "content": "new"}
        assert writes[0].owner_token == new.lease.owner_token
    finally:
        new.finish("released")


def test_plan_wait_remains_running_and_recovery_preserves_attempt(tmp_path):
    from agent_runtime.plan_runtime.recovery import recover
    from agent_runtime.tool_result import ToolResult
    from tests.plan_support import read, session_for, simple_plan, tool

    (tmp_path / "value.py").write_text("value = 1\n")
    with session_for(tmp_path) as session:
        session.create(simple_plan(session))

        def waiting(attempt):
            tool(
                session,
                "read_file",
                {"path": "value.py"},
                lambda: ToolResult(
                    "waiting",
                    status="rejected",
                    error_code="approval_required",
                    metadata={
                        "approval_id": "proposal",
                        "sandbox_mutation_status": "not_started",
                        "termination_guaranteed": True,
                    },
                ),
            )
            return session.tool_result(attempt)

        assert session.run_node("read-0", waiting)["status"] == "waiting_approval"
        attempt_id = session.plan.node("read-0").attempt_id
        assert session.plan.node("read-0").status == "running"
        assert recover(session)["waiting_approval"] == ["read-0"]
        assert session.run_node("read-0", lambda a: read(session, a))["status"] == "success"
        assert session.plan.node("read-0").attempt_id == attempt_id
        assert session.plan.node("read-0").status == "succeeded"
        session.store.validate_execution_history()


@pytest.mark.parametrize("allow", [True, False])
def test_native_approval_preserves_checkpoint_after_deferred_calls(ctx, tmp_path, allow):
    from agent_runtime.agent_loop import AgentLoop
    from agent_runtime.checkpoint import evaluate_resume_state
    from agent_runtime.model_turn import ToolCall
    from tests.test_native_tool_batch import BatchClient

    ctx.sandbox_initial_scope = PermissionScope(write_paths=())
    ctx.sandbox_workspace_versions["a.py"] = DIGEST
    agent = make_agent(ctx, tmp_path)
    client = agent.model_client = BatchClient(
        [
            ToolCall("write_file", {"path": "a.py", "content": "new"}, "approved-call"),
            ToolCall("write_file", {"path": "b.py", "content": "deferred"}, "deferred-call"),
        ]
    )
    loop = AgentLoop(agent)
    assert "审批" in loop.run("edit", skip_plan=True)
    assert evaluate_resume_state(agent)["status"] == "approval-pending"
    observations = agent.session["tool_observations"]
    assert [obs["call_id"] for obs in observations] == ["approved-call", "deferred-call"]
    assert [obs["failure_class"] for obs in observations] == [
        "approval_required",
        "approval_deferred",
    ]
    assert not any(r.tool_name == "write_file" for r in ctx.sandbox_backend.calls)
    agent.resolve_sandbox_approval(agent.pending_sandbox_approval()["id"], allow=allow)
    assert evaluate_resume_state(agent)["status"] == "approval-resumable"
    assert "done" in agent.ask("continue")
    writes = [r for r in ctx.sandbox_backend.calls if r.tool_name == "write_file"]
    assert len(writes) == int(allow)
    if allow:
        assert writes[0].tool_args == {"path": "a.py", "content": "new"}
    assert len(client.requests) == 2


@pytest.mark.parametrize("change", ["owner", "revoke", "cancel", "sealed_args"])
def test_approval_checkpoint_rejects_changed_authority_or_invocation(ctx, tmp_path, change):
    from agent_runtime.agent_loop import AgentLoop
    from agent_runtime.checkpoint import evaluate_resume_state
    from agent_runtime.task_state import TaskState

    ctx.sandbox_initial_scope = PermissionScope(write_paths=())
    ctx.sandbox_workspace_versions["a.py"] = DIGEST
    agent = make_agent(ctx, tmp_path)
    loop = AgentLoop(agent)
    ts = TaskState.create(user_request="edit")
    ts.advance_runtime("reasoning")
    loop._task_state = ts
    loop._run_tool_step(ts, "write_file", {"path": "a.py", "content": "new"}, step=1, path="xml")
    service = permission_service(ctx)
    if change == "owner":
        ctx.sandbox_owner_token = "new-owner"
    elif change == "revoke":
        service.replace_scope(PermissionScope(write_paths=()))
    elif change == "cancel":
        service.cancel_pending()
    else:
        agent.session["checkpoints"][-1]["checkpoint_envelope"]["runtime_control"][
            "sandbox_approval_call"
        ]["args"]["content"] = "tampered"
    assert evaluate_resume_state(agent)["status"] != "approval-resumable"
    assert not any(r.tool_name == "write_file" for r in ctx.sandbox_backend.calls)


def test_l2_cancel_invalidates_pending_before_publishing_recovery(ctx):
    from types import SimpleNamespace

    from src.repair.plan_binding import RepairPlanBinding

    ctx.sandbox_initial_scope = PermissionScope(write_paths=())
    ctx.sandbox_workspace_versions["a.py"] = DIGEST
    waiting = execute_workspace_tool(ctx, "write_file", {"path": "a.py", "content": "new"})
    assert waiting.error_code == "approval_required"
    service = permission_service(ctx)
    events = []

    def cancel():
        assert service.snapshot()["pending"]["status"] == "stale"
        events.append("cancel")
        return "cancel-id"

    binding = SimpleNamespace(
        agent=SimpleNamespace(tool_context=ctx),
        coordinator=SimpleNamespace(request_cancel=cancel),
        publish_recovery=lambda **kwargs: events.append(kwargs["stage"]),
    )
    assert RepairPlanBinding._request_cancel(binding) == "cancel-id"
    assert events == ["cancel", "cancel"]
    assert not ctx.sandbox_backend.calls
    assert service.snapshot()["tickets"] == {}
