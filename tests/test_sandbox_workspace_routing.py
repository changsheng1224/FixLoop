"""Routing and evidence boundaries; fake transport does not prove OS isolation."""

import threading
from copy import copy
from dataclasses import replace
from types import SimpleNamespace

import pytest

from agent_runtime.batch_reads import recheck_read
from agent_runtime.cancellation import CancellationToken
from agent_runtime.code_exploration.consumption import SourceChecks
from agent_runtime.code_exploration.models import RetrievalLimits
from agent_runtime.linux_sandbox.models import SandboxResult
from agent_runtime.linux_sandbox.workspace_contract import tool_result_to_wire
from agent_runtime.linux_sandbox.workspace_routing import execute_workspace_tool
from agent_runtime.tool_context import ToolContext
from agent_runtime.tool_result import ToolResult
from agent_runtime.tools import build_tool_registry
from src.repair.execution.edit_lock import EditLockState
from src.tools.composite import build_repair_canonical_tools
from src.tools.repair_control import tool_expand_lock

DIGEST = "a" * 64


class Backend:
    workspace_tools_only = True

    def __init__(self):
        self.calls = []
        self.handler = self.reply

    def reply(self, request):
        path = request.tool_args.get("path", "a.py")
        result = ToolResult(
            "sandbox evidence",
            data={"bytes_read": 4},
            metadata={
                "workspace_versions": {path: DIGEST} if path != "." else {},
                "source_dependencies": {},
                "read_version": {
                    "path": path,
                    "version": [1, len(self.calls), 4, 0, 0],
                    "authority": "sandbox_workspace",
                },
            },
        )
        if request.tool_name == "workspace_inventory":
            result.data = {"a.py": DIGEST, "b.py": DIGEST}
        return SandboxResult(
            "completed",
            exit_code=0,
            cleanup="confirmed",
            actual_backend="linux_sandbox",
            receipt_id=request.call_id,
            tool_result=tool_result_to_wire(result),
        )

    def execute(self, request):
        self.calls.append(request)
        return self.handler(request)

    def cancel(self, call_id):
        return True


@pytest.fixture
def context(tmp_path):
    def forbidden(*args, **kwargs):
        pytest.fail("host source path was consulted")

    return ToolContext(str(tmp_path), path_resolver=forbidden, sandbox_backend=Backend())


@pytest.mark.parametrize("layer", [1, 2])
def test_registered_reads_and_context_handlers_use_sandbox(context, layer):
    registry = build_tool_registry(context) if layer == 1 else build_repair_canonical_tools(context)
    migrated = (
        {"read_file", "list_files", "grep"}
        if layer == 1
        else {"grep", "ast_parse", "inspect_file", "find_test"}
    )
    for name in migrated:
        spec = registry[name]
        args = {"path": "a.py", "pattern": "a", "file_path": "a.py", "function_name": "a"}
        assert spec["execution_tier"] == "linux_sandbox"
        assert spec["run"](args).ok
        assert spec["run_with_context"](copy(context), args).ok
        assert spec["parallel_run"] is spec["run"]
    assert len(context.sandbox_backend.calls) == 2 * len(migrated)


def test_unmigrated_direct_tools_do_not_spawn_or_create_exploration(context, monkeypatch):
    from agent_runtime import tools
    from src.tools.git_tools import git_blame, git_diff
    from src.tools.java_ast_parser import java_ast_parse

    monkeypatch.setattr(tools, "_exploration_service", lambda *_: pytest.fail("host exploration"))
    for run in (
        tools._code_lookup_structured,
        tools._code_relations_structured,
        tools.tool_run_shell,
        tools.tool_quick_test,
        git_blame,
        git_diff,
        java_ast_parse,
    ):
        assert run(context, {}).error_code == "policy_denied"
    assert execute_workspace_tool(context, "unknown", {}).error_code == "policy_denied"
    assert not context.sandbox_backend.calls


def test_missing_backend_never_uses_host(context):
    context.sandbox_backend = None
    context.sandbox_task_binding = SimpleNamespace(assert_context=lambda *_: None)
    result = execute_workspace_tool(context, "read_file", {"path": "a.py"})
    assert not result.ok


def test_dispatch_serializes_shallow_copies_and_propagates_uncertainty(context):
    entered, release = threading.Event(), threading.Event()
    backend = context.sandbox_backend

    def uncertain(request):
        entered.set()
        assert release.wait(3)
        return SandboxResult("uncertain", receipt_id=request.call_id, cleanup="unverified")

    backend.handler = uncertain
    results = []
    copies = [copy(context), copy(context)]
    threads = [
        threading.Thread(
            target=lambda ctx=ctx: results.append(
                execute_workspace_tool(ctx, "read_file", {"path": "a.py"})
            )
        )
        for ctx in copies
    ]
    threads[0].start()
    assert entered.wait(3)
    threads[1].start()
    release.set()
    for thread in threads:
        thread.join(4)
        assert not thread.is_alive()
    assert len(backend.calls) == 1
    assert context.sandbox_uncertain_event.is_set()
    assert {result.status for result in results} == {"uncertain", "error"}


def test_queued_cancel_never_dispatches(context):
    token = context.cancel_token = CancellationToken()
    result = []
    context.sandbox_dispatch_lock.acquire()
    try:
        thread = threading.Thread(
            target=lambda: result.append(
                execute_workspace_tool(context, "read_file", {"path": "a.py"})
            )
        )
        thread.start()
        token.cancel()
        thread.join(3)
        assert not thread.is_alive()
    finally:
        context.sandbox_dispatch_lock.release()
    assert result[0].status == "cancelled"
    assert result[0].error_code == "cancelled"
    assert not context.sandbox_backend.calls


@pytest.mark.parametrize("key", ["workspace_versions", "source_dependencies"])
def test_invalid_version_evidence_freezes_workspace(context, key):
    backend = context.sandbox_backend

    def forged(request):
        reply = backend.reply(request)
        raw = reply.tool_result
        raw["metadata"][key] = {"../escape.py": DIGEST}
        return replace(reply, tool_result=raw)

    backend.handler = forged
    result = execute_workspace_tool(context, "read_file", {"path": "a.py"})
    assert result.status == "uncertain"
    assert context.sandbox_uncertain_event.is_set()
    assert not context.sandbox_workspace_versions


def test_edit_lock_requires_evidence_and_reread_after_expand(context):
    lock = context.edit_lock = EditLockState(repo_root=context.root, sandbox_only=True)
    assert not lock.mark_read("a.py")
    assert not lock.mark_read_evidence("a.py", ToolResult("untrusted"))
    assert execute_workspace_tool(context, "read_file", {"path": "a.py"}).ok
    context.sandbox_approval_callback = lambda _: True
    assert tool_expand_lock(context, {"path": "a.py"}).ok
    assert "a.py" not in context.sandbox_workspace_versions
    assert not lock.check_write("a.py")[0]
    assert execute_workspace_tool(context, "read_file", {"path": "a.py"}).ok
    assert lock.check_write("a.py")[0]
    assert (
        execute_workspace_tool(
            context, "patch_file", {"path": "a.py", "base_hash": "b" * 64}
        ).error_code
        == "stale_precondition"
    )


def test_backend_exception_finalizes_coordinator_resource(context):
    transitions = []
    context.run_coordinator = SimpleNamespace(
        lease=SimpleNamespace(
            run_id="run", task_id="task", owner_token="owner", generation=1, coordination_revision=1
        ),
        assert_can_dispatch=lambda: None,
        register_resource=lambda **kw: SimpleNamespace(resource_id=kw["resource_id"]),
        transition_resource=lambda *a, **kw: transitions.append((a, kw)),
    )

    def failure(_):
        raise OSError("lost transport")

    context.sandbox_backend.handler = failure
    with pytest.raises(OSError, match="lost transport"):
        execute_workspace_tool(context, "read_file", {"path": "a.py"})
    assert transitions[-1][0][1] == "unknown"
    assert transitions[-1][1]["cleanup"] == "unverified"
    assert context.sandbox_uncertain_event.is_set()


def make_agent(context, tmp_path):
    from agent_runtime.config import AgentConfig
    from agent_runtime.providers.clients import FakeModelClient
    from agent_runtime.runtime import Agent
    from agent_runtime.workspace import WorkspaceContext

    return Agent(
        config=AgentConfig(provider="fake", approval="auto"),
        model_client=FakeModelClient(["<final>done</final>"]),
        workspace=WorkspaceContext(cwd=context.root, repo_root=context.root),
        tool_context=context,
        cwd=context.root,
    )


def test_native_agent_batch_routes_copies_and_freshness_through_sandbox(context, tmp_path):
    from agent_runtime.agent_loop import AgentLoop
    from agent_runtime.config import AgentConfig
    from agent_runtime.model_turn import FinishKind, ModelTurnResult, ProviderFinish, ToolCall
    from agent_runtime.providers.clients import FakeNativeToolClient
    from agent_runtime.runtime import Agent
    from agent_runtime.workspace import WorkspaceContext

    class Client(FakeNativeToolClient):
        def __init__(self):
            super().__init__(["<final>done</final>"])
            self.requests = []

        def complete_turn(self, request):
            self.requests.append(request)
            if len(self.requests) == 1:
                return ModelTurnResult(
                    tool_calls=[
                        ToolCall("read_file", {"path": "a.py"}, "first"),
                        ToolCall("read_file", {"path": "b.py"}, "second"),
                    ],
                    finish=ProviderFinish(FinishKind.TOOL_CALLS),
                )
            return super().complete_turn(request)

    client = Client()
    agent = Agent(
        config=AgentConfig(provider="fake", approval="auto", max_steps=5, loop_detect_threshold=0),
        model_client=client,
        workspace=WorkspaceContext(cwd=context.root, repo_root=context.root),
        tool_context=context,
        cwd=context.root,
    )
    assert "done" in AgentLoop(agent).run("inspect both", skip_plan=True)
    blocks = client.requests[1].messages[-1]["content"]
    assert [block["tool_use_id"] for block in blocks] == ["first", "second"]
    assert all("sandbox evidence" in block["content"] for block in blocks)
    observations = agent.session["tool_observations"]
    assert [item["call_id"] for item in observations] == ["first", "second"]
    assert all(item["status"] == "success" for item in observations)
    calls = context.sandbox_backend.calls
    assert {call.tool_args["path"] for call in calls if call.tool_name == "read_file"} == {
        "a.py",
        "b.py",
    }
    assert any(call.tool_name == "workspace_source_version" for call in calls)
    assert not context.sandbox_uncertain_event.is_set()


@pytest.mark.parametrize(
    "name,args",
    [
        ("write_file", {"path": "a.py", "content": "pass\n"}),
        ("patch_file", {"path": "a.py", "old_text": "old", "new_text": "new"}),
        (
            "apply_patch",
            {"patch": "*** Begin Patch\n*** Update File: a.py\n@@\n-old\n+new\n*** End Patch"},
        ),
    ],
)
def test_executor_writes_never_take_host_snapshots_or_previews(
    context, tmp_path, monkeypatch, name, args
):
    from agent_runtime.tool_executor import ToolExecutor

    agent = make_agent(context, tmp_path)
    executor = ToolExecutor(agent=agent, approval_policy="auto")
    context.sandbox_workspace_versions["a.py"] = DIGEST
    for attr in (
        "_capture_snapshot",
        "_capture_restore_snapshot",
        "_build_patch_preview",
        "_restore_restore_snapshot",
        "_attach_stale_preimage_metadata",
    ):
        monkeypatch.setattr(executor, attr, lambda *_: pytest.fail("host snapshot/preview"))
    result = executor.execute_gated(name, args)
    assert result.ok
    assert result.metadata["workspace_diff_status"] == "broker_receipt"
    assert context.sandbox_backend.calls[-1].tool_name == name


@pytest.mark.parametrize("failure", ["missing", "uncertain", "command", "unknown"])
def test_executor_rejects_before_host_paths_or_snapshots(context, tmp_path, monkeypatch, failure):
    from agent_runtime.tool_executor import ToolExecutor

    agent = make_agent(context, tmp_path)
    executor = ToolExecutor(agent=agent, approval_policy="auto")
    backend = context.sandbox_backend
    for attr in ("_validate_args", "_capture_snapshot", "_build_patch_preview"):
        monkeypatch.setattr(executor, attr, lambda *_: pytest.fail("reached host validation"))
    name = "read_file"
    if failure == "missing":
        context.sandbox_backend = None
        context.sandbox_task_binding = SimpleNamespace()
    elif failure == "uncertain":
        context.sandbox_uncertain_event.set()
    elif failure == "command":
        name = "quick_test"
    else:
        name = "custom_tool"
        agent._tool_names += (name,)
        agent.tools[name] = {"run": lambda *_: pytest.fail("unknown tool executed")}
    result = executor.execute_gated(name, {})
    assert result.status == "rejected"
    assert not backend.calls


@pytest.mark.parametrize("rejected", [False, True])
def test_sandbox_observation_recording_never_hashes_host_source(
    context, tmp_path, monkeypatch, rejected
):
    from pathlib import Path

    from agent_runtime.observation_recording import ToolObservationRecorder
    from agent_runtime.repair_runtime import CanonicalToolCall

    source = tmp_path / "a.py"
    source.write_text("secret source")
    native_open = Path.open

    def guarded_open(path, *a, **kw):
        if path == source:
            pytest.fail("observation hashed host source")
        return native_open(path, *a, **kw)

    monkeypatch.setattr(Path, "open", guarded_open)
    recorder = ToolObservationRecorder(
        session={"id": "session"},
        root=context.root,
        state_root=context.state_root,
        emit=lambda *_: None,
        on_changed_paths=lambda *_: None,
        on_retrieval=lambda *_: None,
        sandbox_bound=True,
    )
    result = ToolResult.error("denied", code="policy_denied") if rejected else ToolResult("read")
    assert recorder.record(
        CanonicalToolCall.create("read_file", {"path": "a.py"}),
        result,
        duration_ms=0,
        metadata={},
        source_version="",
        idempotency_key="k",
    ).stored.status == ("execution_error" if rejected else "success")


def test_context_evidence_freshness_uses_sandbox_and_hides_stale_source(
    context, tmp_path, monkeypatch
):
    from agent_runtime.context_manager import ContextManager
    from agent_runtime.context_runtime import ObservationStore

    agent = make_agent(context, tmp_path)
    store = ObservationStore(agent.session, context.root, context.state_root)
    observation = store.put(
        "read_file",
        {"path": "a.py"},
        "def sandbox_only(): pass",
        source_dependencies={"a.py": DIGEST},
        retrieval_result={"dependency_versions": {"a.py": DIGEST}, "hits": [{"path": "a.py"}]},
    )
    store.close()
    agent.session["tool_observations"] = [{"observation_id": observation.observation_id}]
    manager = ContextManager(agent)
    metadata = {}
    manager._prepare_code_evidence(metadata)
    assert metadata["code_evidence"][observation.observation_id]["ok"]
    assert context.sandbox_backend.calls[-1].tool_name == "workspace_source_version"
    assert not context.sandbox_workspace_versions
    backend = context.sandbox_backend

    def changed(request):
        reply = backend.reply(request)
        reply.tool_result["metadata"]["workspace_versions"] = {"a.py": "b" * 64}
        return reply

    backend.handler = changed
    metadata = {}
    manager._prepare_code_evidence(metadata)
    view = metadata["code_evidence"][observation.observation_id]
    assert not view["ok"] and view["reason"] == "source_changed"
    assert "def sandbox_only" not in str(view)


def test_source_checks_share_budget_without_marking_read_or_write_base(context):
    checks = SourceChecks(context)
    assert checks.check({"a.py": DIGEST}, context.root) == ("fresh", "")
    assert checks.check({"a.py": DIGEST}, context.root) == ("fresh", "")
    assert checks.bytes_read == 4 and checks.checked_files == 1
    assert len(context.sandbox_backend.calls) == 1
    assert context.sandbox_backend.calls[0].tool_name == "workspace_source_version"
    assert not context.sandbox_workspace_versions
    assert checks.check({"a.py": "b" * 64}, context.root) == ("stale", "source_changed")
    assert checks.check({"../secret": DIGEST}, context.root) == ("stale", "source_policy_changed")


def test_source_checks_budget_is_sent_to_worker_and_stops_next_file(context):
    context.exploration_limits = RetrievalLimits(file_hash_bytes=4, search_read_bytes=4)
    checks = SourceChecks(context)
    assert checks.check({"a.py": DIGEST}, context.root) == ("fresh", "")
    backend = context.sandbox_backend
    backend.handler = lambda req: replace(
        backend.reply(req),
        tool_result=tool_result_to_wire(
            ToolResult.error("source too large", code="output_too_large")
        ),
    )
    assert checks.check({"b.py": DIGEST}, context.root) == ("unknown", "source_byte_budget")
    assert backend.calls[-1].tool_args["max_bytes"] == 0


def test_batch_freshness_uses_hash_across_fuse_mounts_and_invalidates_stale_base(context):
    result = execute_workspace_tool(context, "read_file", {"path": "a.py"}, mark_read=False)
    assert recheck_read(result, context).ok  # mount-local inode values differ
    backend = context.sandbox_backend

    def changed(req):
        reply = backend.reply(req)
        reply.tool_result["metadata"]["workspace_versions"] = {"a.py": "b" * 64}
        return reply

    backend.handler = changed
    assert recheck_read(result, context).status == "partial"
    assert "a.py" not in context.sandbox_workspace_versions
