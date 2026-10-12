"""S4 real filesystem/runtime acceptance without remote providers."""

import threading
from contextlib import closing, contextmanager
from pathlib import Path

import pytest

from agent_runtime.cancellation import (
    BlockingScope,
    CancellationToken,
    CancelledError,
    run_blocking,
)
from agent_runtime.providers.clients import FakeModelClient
from src.repair_factory import wire_orchestrator
from src.state import RepairState, VerificationResult
from src.workbench.files import ProjectFiles
from src.workbench.models import ConflictError
from src.workbench.project import ProjectExecutor, ProjectResult, readonly_tools
from tests.test_workbench_service import Echo
from tests.workbench_unit_service import WorkbenchService


@contextmanager
def workspace(tmp_path, executor):
    repo = tmp_path / "repo"
    repo.mkdir(exist_ok=True)
    (repo / "v.py").write_text("value = 1\n", encoding="utf-8")
    with WorkbenchService(
        tmp_path / "state",
        executor=Echo(),
        project_executor=executor,
        project_lock_dir=tmp_path / "locks",
    ) as service:
        yield service, service.create_conversation(project_path=str(repo)), repo


class Editor:
    def __init__(self):
        self.calls = []

    def __call__(self, **kw):
        self.calls.append(kw)
        request = kw["messages"][-1]["content"]
        (kw["files"].root / "v.py").write_text(request, encoding="utf-8")
        kw["on_chunk"]("done")
        return ProjectResult("done")


def complete(service, cid, text, mode="repair"):
    turn = service.send(cid, text, mode=mode)
    run = service.wait(cid, turn.run.id, timeout=20)
    assert run.status == "succeeded", run
    assert run.output_snapshot_id
    return turn


def test_three_round_edit_reverses_later_code_preserves_manual_future(tmp_path):
    executor = Editor()
    with workspace(tmp_path, executor) as (service, chat, repo):
        first = complete(service, chat.id, "one")
        complete(service, chat.id, "two")
        third = complete(service, chat.id, "three")
        (repo / "v.py").write_text("manual", encoding="utf-8")
        original = service.store.get_tree(chat.id)
        preview = service.preview_restore(chat.id, action="input", node_id=first.node.id)
        with pytest.raises(ConflictError):
            service.edit(chat.id, first.node.id, "edited")
        assert service.store.get_tree(chat.id) == original
        turn = service.edit(chat.id, first.node.id, "edited", plan_id=preview["id"])
        assert service.wait(chat.id, turn.run.id).status == "succeeded"
        assert executor.calls[-1]["messages"] == ({"role": "user", "content": "edited"},)
        tree = service.store.get_tree(chat.id)
        before = next(s for s in tree.snapshots if s.id == turn.node.input_snapshot_id)
        assert (
            service.restoration.repository.blob(
                before.manifest["entries"]["v.py"]["digest"]
            ).replace(b"\r\n", b"\n")
            == b"value = 1\n"
        )
        assert service.store.get_run(chat.id, third.run.id) == original.runs[-1]
        assert executor.calls[0]["state_root"] != executor.calls[-1]["state_root"]
        switch = service.preview_restore(chat.id, action="switch", branch_id=chat.active_branch_id)
        service.apply_restore(chat.id, switch["id"])
        assert (repo / "v.py").read_text(encoding="utf-8") == "manual"


def test_input_capture_includes_manual_checkpoint_and_chat_in_project(tmp_path):
    with workspace(tmp_path, Editor()) as (service, chat, repo):
        (repo / "v.py").write_text("manual", encoding="utf-8")
        turn = complete(service, chat.id, "ordinary", "chat")
        tree = service.store.get_tree(chat.id)
        assert tree.nodes[0].kind == "local_checkpoint"
        assert turn.node.parent_id == tree.nodes[0].id
        assert (repo / "v.py").read_text(encoding="utf-8") == "manual"
        assert service.store.get_run(chat.id, turn.run.id).artifacts["changed_files"] == []


@pytest.mark.parametrize(
    "path", [".env", "../secret", "C:/secret", ".git/config", ".agent/history.jsonl"]
)
def test_readonly_tools_reject_private_and_escaped_paths(tmp_path, path):
    (tmp_path / "v.py").write_text("value = 1", encoding="utf-8")
    with closing(ProjectFiles(tmp_path)) as files:
        tools = readonly_tools(files, CancellationToken())
        assert set(tools) == {"read_file", "list_files", "grep"}
        with pytest.raises((ValueError, FileNotFoundError, PermissionError)):
            tools["read_file"]["run"]({"path": path})
        assert tools["grep"]["run"]({"pattern": "value"}).content == "v.py:1: value = 1"


def test_real_l1_question_reads_file_fresh_each_turn(tmp_path):
    clients = []

    def factory():
        client = FakeModelClient(
            [
                '<tool>{"name":"read_file","args":{"path":"v.py"}}</tool>',
                "<final>value is 1</final>",
            ]
        )
        clients.append(client)
        return client

    executor = ProjectExecutor(factory, provider="fake")
    with workspace(tmp_path, executor) as (service, chat, repo):
        first = complete(service, chat.id, "explain v.py", "project_chat")
        run = service.store.get_run(chat.id, first.run.id)
        assert run.answer == "value is 1"
        assert run.artifacts["changed_files"] == []
        assert "value = 1" in clients[0].prompts[-1]
        assert not (repo / ".agent").exists()
        events = service.events(chat.id, run.id)
        assert any(e["kind"] == "project.progress" for e in events)
        plan = service.preview_restore(chat.id, action="input", node_id=first.node.id)
        next_turn = service.regenerate(chat.id, first.node.id, plan_id=plan["id"])
        assert service.wait(chat.id, next_turn.run.id, timeout=20).status == "succeeded"
        assert len(clients) == 2
        assert "value is 1" not in clients[1].prompts[0]


@pytest.mark.parametrize("cancel", [False, True])
def test_partial_files_captured_on_failure_and_cancel(tmp_path, cancel):
    started, release = threading.Event(), threading.Event()

    def executor(**kw):
        (kw["files"].root / "v.py").write_text("partial", encoding="utf-8")
        kw["on_chunk"]("partial answer")
        started.set()
        assert release.wait(5)
        kw["token"].check()
        raise RuntimeError("secret transport error")

    with workspace(tmp_path, executor) as (service, chat, repo):
        turn = service.send(chat.id, "repair", mode="repair")
        assert started.wait(5)
        with pytest.raises(ConflictError):
            service.preview_restore(chat.id, action="input", node_id=turn.node.id)
        if cancel:
            service.stop(chat.id, turn.run.id)
        release.set()
        run = service.wait(chat.id, turn.run.id)
        assert run.status == ("cancelled" if cancel else "failed")
        assert run.output_snapshot_id and run.answer == "partial answer"
        assert run.artifacts["changed_files"] == ["v.py"]
        assert "secret" not in (run.error or "")


def test_wrong_node_stale_revision_and_editor_change_never_restore(tmp_path):
    with workspace(tmp_path, Editor()) as (service, chat, repo):
        first = complete(service, chat.id, "one")
        second = complete(service, chat.id, "two")
        plan = service.preview_restore(chat.id, action="input", node_id=first.node.id)
        tree = service.store.get_tree(chat.id)
        for node, revision in ((second.node.id, None), (first.node.id, 0)):
            with pytest.raises(ConflictError):
                service.regenerate(chat.id, node, plan_id=plan["id"], expected_revision=revision)
            assert service.store.get_tree(chat.id) == tree
        (repo / "v.py").write_text("editor", encoding="utf-8")
        with pytest.raises(ConflictError):
            service.edit(chat.id, first.node.id, "new", plan_id=plan["id"])
        assert service.store.get_tree(chat.id) == tree
        assert (repo / "v.py").read_text(encoding="utf-8") == "editor"


def test_scope_drains_cancelled_nested_writer(tmp_path):
    token = CancellationToken()
    started, release, finished = threading.Event(), threading.Event(), threading.Event()

    def write():
        started.set()
        assert release.wait(5)
        (tmp_path / "late").write_text("done", encoding="utf-8")

    def worker():
        with BlockingScope():
            try:
                run_blocking(write, cancel_token=token)
            except CancelledError:
                pass
        finished.set()

    thread = threading.Thread(target=worker)
    thread.start()
    assert started.wait(5)
    token.cancel("user")
    assert not finished.wait(0.2)
    release.set()
    thread.join(5)
    assert finished.is_set() and (tmp_path / "late").read_text(encoding="utf-8") == "done"


@pytest.mark.parametrize("verified", [True, False])
def test_repair_adapter_requires_l2_fixed_and_verification(tmp_path, verified):
    calls = []

    def factory(client, root, **kw):
        class Orch:
            _active_agents = ()

            def repair(self, issue, **options):
                calls.append((issue, options, kw))
                (Path(root) / "v.py").write_text("changed", encoding="utf-8")
                return RepairState(
                    issue_input=issue,
                    status="fixed",
                    verification_result=VerificationResult(all_passed=verified),
                )

        return Orch()

    executor = ProjectExecutor(lambda: object(), repair_factory=factory)
    with workspace(tmp_path, executor) as (service, chat, repo):
        turn = service.send(chat.id, "fix", mode="repair")
        run = service.wait(chat.id, turn.run.id)
        assert run.status == ("succeeded" if verified else "failed")
        assert run.artifacts["verified"] is verified
        assert run.artifacts["changed_files"] == ["v.py"]
        assert calls[0][1]["repair_timeout_s"] == 0
        assert calls[0][1]["run_id"] == run.id


def test_restart_captures_actual_interrupted_project_without_replay(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "v.py").write_text("base", encoding="utf-8")
    with WorkbenchService(tmp_path / "state", project_lock_dir=tmp_path / "locks") as service:
        chat = service.create_conversation(project_path=str(repo))
        turn = service.store.append_turn(chat.id, "old", mode="repair")
        service.store.start_run(chat.id, turn.run.id)
        (repo / "v.py").write_text("partial", encoding="utf-8")

    def forbidden(**kw):
        pytest.fail("restart replayed execution")

    with WorkbenchService(
        tmp_path / "state", project_lock_dir=tmp_path / "locks", project_executor=forbidden
    ) as service:
        run = service.store.get_run(chat.id, turn.run.id)
        assert run.status == "interrupted" and not run.output_snapshot_id
        assert run.error == "execution_cleanup_unconfirmed"
        assert (repo / "v.py").read_text(encoding="utf-8") == "partial"


def test_real_l2_repair_edits_disk_and_runs_local_verification(tmp_path, monkeypatch):
    monkeypatch.setenv("FIXLOOP_USE_WORKTREE", "1")
    client = FakeModelClient(
        [
            '{"conclusion":"v.py initializes value to 1; change it to 2."}',
            '<tool>{"name":"read_file","args":{"path":"v.py"}}</tool>',
            '<tool>{"name":"patch_file","args":{"path":"v.py","old_text":"value = 1","new_text":"value = 2"}}</tool>',
            "<final>Updated value in v.py.</final>",
        ]
    )
    # Explicit trusted unit adapter exercises L2 host verification; the production
    # service rejects host workers and uses the guarded sandbox acceptance path.
    executor = ProjectExecutor(
        lambda: client,
        provider="fake",
        timeout_s=60,
        execution_tier="host",
        repair_factory=wire_orchestrator,
    )
    with workspace(tmp_path, executor) as (service, chat, repo):
        (repo / "test_value.py").write_text(
            "from v import value\ndef test_value():\n    assert value == 2\n", encoding="utf-8"
        )
        turn = service.send(chat.id, "Change the initial value in v.py from 1 to 2.", mode="repair")
        run = service.wait(chat.id, turn.run.id, timeout=65)
        assert run.status == "succeeded", run
        assert run.artifacts["verified"] is True
        assert run.artifacts["verification"]["passed"] >= 1
        assert run.artifacts["changed_files"] == ["v.py"]
        assert (repo / "v.py").read_text(encoding="utf-8") == "value = 1\n"
        plan = service.preview_candidate(chat.id, run.id)
        service.apply_restore(chat.id, plan["id"])
        assert (repo / "v.py").read_text(encoding="utf-8") == "value = 2\n"
        assert not (repo / ".agent").exists()


def test_service_cancel_waits_for_late_writer_before_output_capture(tmp_path):
    started, release = threading.Event(), threading.Event()

    def executor(**kw):
        def late_write():
            started.set()
            assert release.wait(5)
            (kw["files"].root / "v.py").write_text("late", encoding="utf-8")

        with BlockingScope():
            run_blocking(late_write, cancel_token=kw["token"])

    with workspace(tmp_path, executor) as (service, chat, repo):
        turn = service.send(chat.id, "repair", mode="repair")
        assert started.wait(5)
        service.stop(chat.id, turn.run.id)
        with pytest.raises(TimeoutError):
            service.wait(chat.id, turn.run.id, timeout=0.2)
        with pytest.raises(ConflictError):
            service.preview_restore(chat.id, action="input", node_id=turn.node.id)
        release.set()
        run = service.wait(chat.id, turn.run.id)
        assert run.status == "cancelled" and run.output_snapshot_id
        assert run.artifacts["changed_files"] == ["v.py"]
        tree = service.store.get_tree(chat.id)
        output = next(s for s in tree.snapshots if s.id == run.output_snapshot_id)
        assert (
            service.restoration.repository.blob(output.manifest["entries"]["v.py"]["digest"])
            == b"late"
        )


def test_output_capture_failure_blocks_history_until_restart(tmp_path, monkeypatch):
    with workspace(tmp_path, Editor()) as (service, chat, repo):
        save = service.restoration.repository.save
        calls = 0

        def flaky(files, **kwargs):
            nonlocal calls
            calls += 1
            if calls == 2:
                raise OSError("disk unavailable")
            return save(files, **kwargs)

        monkeypatch.setattr(service.restoration.repository, "save", flaky)
        turn = service.send(chat.id, "partial", mode="repair")
        with pytest.raises(TimeoutError):
            service.wait(chat.id, turn.run.id, timeout=0.5)
        assert service.store.get_run(chat.id, turn.run.id).status == "running"
        with pytest.raises(ConflictError):
            service.send(chat.id, "another", mode="repair")
        with pytest.raises(ConflictError):
            service.preview_restore(chat.id, action="input", node_id=turn.node.id)
    with WorkbenchService(tmp_path / "state", project_lock_dir=tmp_path / "locks") as service:
        run = service.store.get_run(chat.id, turn.run.id)
        assert run.status == "interrupted" and run.output_snapshot_id
        assert (repo / "v.py").read_text(encoding="utf-8") == "value = 1\n"


def test_project_dispatch_failure_commits_actual_output_snapshot(tmp_path, monkeypatch):
    with workspace(tmp_path, Editor()) as (service, chat, repo):

        def fail_start(_):
            raise RuntimeError("dispatch failed")

        monkeypatch.setattr(threading.Thread, "start", fail_start)
        with pytest.raises(RuntimeError, match="dispatch failed"):
            service.send(chat.id, "fix", mode="repair")
        tree = service.store.get_tree(chat.id)
        assert tree.runs[-1].status == "interrupted"
        assert tree.runs[-1].output_snapshot_id
        assert (repo / "v.py").read_text(encoding="utf-8") == "value = 1\n"


def test_external_runtime_does_not_recall_project_memory(tmp_path):
    from agent_runtime.config import AgentConfig
    from agent_runtime.context_manager import ContextManager
    from agent_runtime.features.memory.durable import DurableMemoryStore
    from agent_runtime.runtime import Agent
    from agent_runtime.tool_context import ToolContext
    from agent_runtime.workspace import WorkspaceContext

    repo = tmp_path / "repo"
    repo.mkdir()
    store = DurableMemoryStore(str(repo))
    store.promote([("key-decisions", "explain v.py OLD_FUTURE_SENTINEL")])
    assert store.retrieval("explain v.py")
    ws = WorkspaceContext(cwd=str(repo), repo_root=str(repo), doc_contents={}, dirty_file_hashes={})
    agents = [
        Agent(
            AgentConfig(provider="fake"),
            FakeModelClient([]),
            ws,
            cwd=str(repo),
            tools={},
            tool_context=ToolContext(root=str(repo), state_root=state),
        )
        for state in ("", str(tmp_path / "new-run"))
    ]
    try:
        assert "OLD_FUTURE_SENTINEL" in ContextManager(agents[0])._get_knowledge("explain v.py")
        assert "OLD_FUTURE_SENTINEL" not in ContextManager(agents[1])._get_knowledge("explain v.py")
    finally:
        for agent in agents:
            agent.close()
