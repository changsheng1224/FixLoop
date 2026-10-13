"""Project lineage survives execution copies without sharing mutable run resources."""

import json
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

from agent_runtime.linux_sandbox.task_context import (
    SandboxEnvironmentIdentity,
    SandboxTaskBinding,
    SandboxTaskSpec,
)
from agent_runtime.state_root import managed_state_base, project_id, workspace_id
from agent_runtime.tool_context import ToolContext
from agent_runtime.worktree import (
    _git,
    adopt_worktree,
    create_worktree,
    remove_worktree,
    retain_worktree,
)
from src.workbench.project import ProjectExecutor, ProjectResult
from src.workbench.store import WorkbenchStore
from src.workbench.supervisor import reconcile_sandbox
from tests.test_workbench_service import Echo
from tests.workbench_unit_service import WorkbenchService


@pytest.fixture
def spec(tmp_path):
    return SandboxTaskSpec(
        SandboxEnvironmentIdentity("FixLoop-Test", "1", "1", "a" * 64),
        "identity-v1",
        "b" * 64,
        str(tmp_path / "shared-state"),
    )


@pytest.mark.parametrize("environment_root", [False, True])
def test_independent_clones_can_use_same_run_id_in_shared_storage(
    temp_workspace,
    tmp_path,
    spec,
    monkeypatch,
    environment_root,
):
    clone = tmp_path / "clone"
    _git(temp_workspace, "clone", str(temp_workspace), str(clone))
    assert (
        _git(clone, "rev-parse", "HEAD").stdout == _git(temp_workspace, "rev-parse", "HEAD").stdout
    )
    if environment_root:
        monkeypatch.setenv("FIXLOOP_STATE_ROOT", spec.state_base)
        spec = replace(spec, state_base="")

    def allocate(root):
        binding = SandboxTaskBinding.create(root, root, "same-run", spec)
        binding.save()
        Path(binding.state_root, "private.txt").write_text(root.name)
        return binding

    with ThreadPoolExecutor(max_workers=2) as pool:
        first, second = pool.map(allocate, (temp_workspace, clone))
    assert first.project_id != second.project_id
    assert first.state_root != second.state_root
    for root, binding in ((temp_workspace, first), (clone, second)):
        assert SandboxTaskBinding.load(root, "same-run", spec) == binding
        assert Path(binding.state_root, "private.txt").read_text() == root.name
    # Ownership records and Git branches may also reuse the run name across projects.
    one = create_worktree(temp_workspace, "same-run", state_base=spec.state_base or None)
    two = create_worktree(clone, "same-run", state_base=spec.state_base or None)
    assert one.project_id == first.project_id and two.project_id == second.project_id
    assert one.path != two.path
    retain_worktree(one)
    assert remove_worktree(temp_workspace, one.path, state_base=one.state_base)
    assert two.path.exists()


def test_worktree_runs_share_project_but_not_workspace_or_state(temp_workspace, spec):
    handles = [
        create_worktree(temp_workspace, run, state_base=spec.state_base) for run in ("a", "b")
    ]
    bindings = []
    for handle in handles:
        binding = SandboxTaskBinding.create(
            temp_workspace,
            handle.path,
            handle.run_id,
            spec,
            ownership="task",
            branch=handle.branch,
        )
        binding.save()
        bindings.append(binding)
        assert binding.project_id == handle.project_id == project_id(temp_workspace)
        assert binding.mapping.workspace_id == handle.workspace_id
        assert SandboxTaskBinding.load(temp_workspace, handle.run_id, spec) == binding
        assert (
            adopt_worktree(
                temp_workspace,
                handle.path,
                handle.run_id,
                state_base=handle.state_base,
                ownership="task",
                branch=handle.branch,
            )
            == handle
        )
    assert bindings[0].state_root != bindings[1].state_root
    assert bindings[0].mapping.workspace_id != bindings[1].mapping.workspace_id
    before = bindings[0].project_id
    (handles[0].path / "README.md").write_text("uncommitted change")
    bindings[0].validate()
    assert bindings[0].project_id == before


def test_source_directory_replacement_invalidates_execution_copy(tmp_path, spec):
    source, copy = tmp_path / "source", tmp_path / "copy"
    source.mkdir()
    copy.mkdir()
    spec = replace(spec, project_root=str(source))
    binding = SandboxTaskBinding.create(copy, copy, "run", spec)
    path = binding.save()
    source.rename(tmp_path / "original-source")
    source.mkdir()
    with pytest.raises(ValueError, match="project identity changed"):
        binding.validate()
    with pytest.raises(ValueError, match="project identity changed"):
        SandboxTaskBinding.load_record(path)
    with pytest.raises(ValueError, match="identity_mismatch"):
        SandboxTaskBinding.load(copy, "run", spec)


def test_resume_cannot_reassign_project_or_context(tmp_path, spec):
    source, other, copy = (tmp_path / name for name in ("source", "other", "copy"))
    for root in (source, other, copy):
        root.mkdir()
    source_spec = replace(spec, project_root=str(source))
    binding = SandboxTaskBinding.create(copy, copy, "run", source_spec)
    path = binding.save()
    target = managed_state_base(other, spec.state_base) / "tasks" / "run" / path.name
    target.parent.mkdir(parents=True)
    target.write_bytes(path.read_bytes())
    with pytest.raises(ValueError, match="identity_mismatch"):
        SandboxTaskBinding.load(copy, "run", replace(spec, project_root=str(other)))
    with pytest.raises(ValueError, match="project identity changed"):
        replace(binding, project_id=project_id(other)).validate()
    context = ToolContext(str(copy))
    binding.attach(context)
    context.sandbox_project_id = project_id(other)
    with pytest.raises(ValueError, match="identity_mismatch"):
        binding.assert_context(context)


def test_workbench_records_canonical_source_for_concurrent_copies(tmp_path):
    roots = [tmp_path / name for name in ("first", "second")]
    for root in roots:
        root.mkdir()
    calls = []

    def execute(**kw):
        calls.append(kw)
        return ProjectResult("done")

    with WorkbenchService(
        tmp_path / "workbench",
        executor=Echo(),
        project_executor=execute,
        project_lock_dir=tmp_path / "locks",
    ) as service:
        chats = [service.create_conversation(project_path=str(root)) for root in (*roots, roots[0])]
        turns = [service.send(chat.id, "question", mode="project_chat") for chat in chats]
        for turn in turns:
            assert service.wait(turn.run.conversation_id, turn.run.id).status == "succeeded"
        resources = [service.store.resource(turn.run.id) for turn in turns]
        assert resources[0]["project_id"] == resources[2]["project_id"] == project_id(roots[0])
        assert resources[0]["project_id"] != resources[1]["project_id"]
        assert len({r["workspace_id"] for r in resources}) == 3
        assert len({r["state_root"] for r in resources}) == 3
        for resource in resources:
            call = next(c for c in calls if c["run_id"] == resource["run_id"])
            assert call["project_root"] == resource["project_path"]
            assert workspace_id(call["files"].root) == resource["workspace_id"]
        database = service.store.database
    reopened = WorkbenchStore(database)
    assert reopened.project_record(chats[0].id)["project_id"] == resources[0]["project_id"]
    independent_store = WorkbenchStore(tmp_path / "other-store.sqlite3")
    chat = independent_store.create_conversation(project_path=str(roots[0]))
    assert independent_store.project_record(chat.id)["project_id"] == resources[0]["project_id"]


def test_repair_adapter_carries_source_into_factory(tmp_path):
    from contextlib import closing

    from agent_runtime.cancellation import CancellationToken
    from src.state import RepairState, VerificationResult
    from src.workbench.files import ProjectFiles

    source, copy = tmp_path / "source", tmp_path / "copy"
    source.mkdir()
    copy.mkdir()
    calls = []

    def factory(client, root, **kw):
        calls.append((root, kw))

        class Orch:
            session = SimpleNamespace(active_agents=(), progress=None)

            def run(self, request):
                return RepairState(
                    issue_input="fix",
                    status="fixed",
                    verification_result=VerificationResult(
                        all_passed=True, total_tests=1, passed=1, execution_status="passed"
                    ),
                )

        return Orch()

    with closing(ProjectFiles(copy)) as files:
        result = ProjectExecutor(lambda: object(), repair_factory=factory)(
            mode="repair",
            files=files,
            messages=[{"role": "user", "content": "fix"}],
            token=CancellationToken(),
            on_chunk=lambda _: None,
            publish=lambda *a: None,
            run_id="run",
            state_root=tmp_path / "runtime",
            project_root=str(source),
        )
    assert result.status == "succeeded"
    assert calls[0][0] == str(copy)
    assert calls[0][1]["project_root"] == str(source)


def test_published_project_key_is_preserved_with_canonical_runtime_identity(tmp_path, monkeypatch):
    import src.workbench.store as module

    source = tmp_path / "source"
    source.mkdir()
    database = tmp_path / "workbench.sqlite3"
    with monkeypatch.context() as patch:
        patch.setattr(module, "project_id", lambda _: "project-legacy-random-key")
        store = WorkbenchStore(database)
        first = store.create_conversation(project_path=str(source))
    reopened = WorkbenchStore(database)
    second = reopened.create_conversation(project_path=str(source))
    for chat in (first, second):
        record = reopened.project_record(chat.id)
        assert record["id"] == "project-legacy-random-key"
        assert record["project_id"] == project_id(source)
    assert reopened.get_conversation(first.id) == first


def test_cleanup_inspects_namespaced_binding_and_rejects_wrong_owner(tmp_path, spec, monkeypatch):
    import agent_runtime.linux_sandbox.workspace_backend as backend

    source, copy = tmp_path / "source", tmp_path / "copy"
    source.mkdir()
    copy.mkdir()
    binding = SandboxTaskBinding.create(copy, copy, "run", replace(spec, project_root=str(source)))
    path = binding.save()
    seen = []

    def inspect(loaded, *, retirement_only):
        seen.append(loaded)
        assert retirement_only
        return SimpleNamespace(
            store=SimpleNamespace(current=lambda: {"call_id": "call"}),
            inspect_receipt=lambda _: {"state": "running"},
        )

    monkeypatch.setattr(backend, "GuardedWindowsWorkspaceBackend", inspect)
    resource = {
        "state_root": spec.state_base,
        "project_path": str(source),
        "project_id": binding.project_id,
        "root_identity": list(binding.project_directory_identity),
        "workspace": str(copy),
        "workspace_id": binding.mapping.workspace_id,
        "run_id": "run",
    }
    assert reconcile_sandbox(resource) is False
    assert seen == [binding]  # A misplaced lookup would incorrectly return True for absence.
    for change in (
        {"project_id": "other"},
        {"workspace_id": "other"},
        {"root_identity": []},
        {"run_id": "../run"},
    ):
        assert reconcile_sandbox({**resource, **change}) is False
    raw = json.loads(path.read_text())
    raw["project_id"] = "other"
    path.write_text(json.dumps(raw))
    assert reconcile_sandbox(resource) is False


def test_missing_cleanup_identity_keeps_gate_closed(tmp_path):
    assert reconcile_sandbox({"workspace": str(tmp_path / "copy")}) is False
