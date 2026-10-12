"""S3 real filesystem acceptance; project runs below simulate the S4 boundary."""

import os
import stat
import subprocess
import sys
import threading
from contextlib import contextmanager

import pytest

from src.workbench.files import PRIVATE
from src.workbench.models import ConflictError, NotFoundError
from src.workbench.service import UnsupportedModeError, WorkbenchService
from src.workbench.snapshots import SnapshotRepository
from tests.test_workbench_http import request
from tests.test_workbench_service import Echo


@contextmanager
def service_at(tmp_path, **kwargs):
    with WorkbenchService(
        tmp_path / "state", executor=Echo(), project_lock_dir=tmp_path / "locks", **kwargs
    ) as service:
        yield service


@pytest.fixture
def workspace(tmp_path):
    repo = tmp_path / "project"
    repo.mkdir()
    (repo / "code.py").write_bytes(b"base\n")
    (repo / "image.bin").write_bytes(bytes(range(256)))
    with service_at(tmp_path) as service:
        chat = service.create_conversation("代码", project_path=str(repo))
        yield service, chat, repo


def simulated_turn(service, chat, repo, text, data=None, status="succeeded"):
    turn = service.store.append_turn(chat.id, text, mode="repair")
    service.store.start_run(chat.id, turn.run.id)
    if data is not None:
        (repo / "code.py").write_bytes(data)
    files = service.restoration.project(chat.id)[1]
    key, manifest = service.restoration.repository.save(files)
    snapshot = service.store.record_snapshot(chat.id, key, manifest=manifest)
    service.store.finish_run(
        chat.id, turn.run.id, status=status, answer="完成", output_snapshot_id=snapshot.id
    )
    return turn


def apply(service, chat, **kwargs):
    plan = service.preview_restore(chat.id, **kwargs)
    service.apply_restore(chat.id, plan["id"])
    return plan


def test_edit_first_input_reverses_third_and_preserves_manual_future(workspace):
    service, chat, repo = workspace
    first = simulated_turn(service, chat, repo, "一")
    simulated_turn(service, chat, repo, "二")
    third = simulated_turn(service, chat, repo, "三", b"third\n")
    (repo / "manual.txt").write_bytes(b"user edit")
    (repo / "code.py").write_bytes(b"third plus manual\n")
    original = service.store.get_tree(chat.id)
    plan = service.preview_restore(chat.id, action="input", node_id=first.node.id)
    assert plan["manual"] and {c["path"] for c in plan["changes"]} == {"code.py", "manual.txt"}
    assert service.store.get_tree(chat.id) == original
    service.apply_restore(chat.id, plan["id"])
    assert (repo / "code.py").read_bytes() == b"base\n"
    assert not (repo / "manual.txt").exists()
    tree = service.store.get_tree(chat.id)
    original_branch = next(b for b in tree.branches if b.id == chat.active_branch_id)
    checkpoint = next(n for n in tree.nodes if n.id == original_branch.head_node_id)
    assert checkpoint.parent_id == third.node.id and checkpoint.user_text == "本地修改"
    assert all(n in tree.nodes for n in original.nodes)
    assert all(r in tree.runs for r in original.runs)
    assert service.store.get_messages(chat.id, tree.conversation.view_node_id) == ()
    assert service.store.get_effective_snapshot(chat.id, tree.conversation.view_node_id) is not None
    apply(service, chat, action="switch", branch_id=chat.active_branch_id)
    assert (repo / "code.py").read_bytes() == b"third plus manual\n"
    assert (repo / "manual.txt").read_bytes() == b"user edit"
    assert not list(repo.glob(PRIVATE + "*"))


def test_failed_baseline_releases_unregistered_project_lock(tmp_path):
    repo = tmp_path / "project"
    repo.mkdir()
    (repo / "a").write_bytes(b"base")
    with service_at(tmp_path) as first:
        with pytest.raises(ValueError):
            first.create_conversation("", project_path=str(repo))
        with WorkbenchService(
            tmp_path / "second-state", executor=Echo(), project_lock_dir=tmp_path / "locks"
        ) as second:
            chat = second.create_conversation("retry", project_path=str(repo))
            assert chat.base_snapshot_id is not None


def test_view_fork_root_and_failed_partial_output(workspace):
    service, chat, repo = workspace
    first = simulated_turn(service, chat, repo, "一", b"one")
    second = simulated_turn(service, chat, repo, "二", b"partial", status="failed")
    service.view(chat.id, first.node.id)
    assert (repo / "code.py").read_bytes() == b"partial"
    apply(service, chat, action="fork", node_id=first.node.id)
    assert (repo / "code.py").read_bytes() == b"one"
    apply(service, chat, action="fork", node_id=second.node.id)
    assert (repo / "code.py").read_bytes() == b"partial"
    apply(service, chat, action="fork", node_id=None)
    assert (repo / "code.py").read_bytes() == b"base\n"


def test_binary_empty_files_directory_and_type_transitions(workspace):
    service, chat, repo = workspace
    (repo / "empty").write_bytes(b"")
    (repo / "folder").mkdir()
    (repo / "folder" / "a").write_bytes(b"before")
    (repo / "swap").write_bytes(b"file")
    saved = service.save_checkpoint(chat.id)
    (repo / "folder" / "a").unlink()
    (repo / "folder").rmdir()
    (repo / "folder").write_bytes(b"now a file")
    (repo / "swap").unlink()
    (repo / "swap").mkdir()
    (repo / "swap" / "child").write_bytes(b"new")
    (repo / "image.bin").write_bytes(b"\x00\xff" * 70000)
    (repo / "empty").unlink()
    apply(service, chat, action="fork", node_id=saved.id)
    assert (repo / "folder" / "a").read_bytes() == b"before"
    assert (repo / "swap").read_bytes() == b"file"
    assert (repo / "image.bin").read_bytes() == bytes(range(256))
    assert (repo / "empty").read_bytes() == b""
    assert not list(repo.glob(PRIVATE + "*"))


@pytest.mark.parametrize("kind", ["file", "history"])
def test_stale_plan_has_no_writes_or_history_mutation(workspace, kind):
    service, chat, repo = workspace
    simulated_turn(service, chat, repo, "change", b"changed")
    plan = service.preview_restore(chat.id, action="fork")
    if kind == "file":
        (repo / "code.py").write_bytes(b"new manual")
    else:
        service.rename_conversation(chat.id, "renamed")
    tree = service.store.get_tree(chat.id)
    before = (repo / "code.py").read_bytes()
    with pytest.raises(ConflictError):
        service.apply_restore(chat.id, plan["id"])
    assert service.store.get_tree(chat.id) == tree
    assert (repo / "code.py").read_bytes() == before
    assert not service.restoration.pending(chat.id)


def test_plan_id_conversation_binding_and_capacity(workspace):
    service, chat, _ = workspace
    plan = service.preview_restore(chat.id, action="fork")
    other = service.create_conversation()
    with pytest.raises((NotFoundError, ValueError)):
        service.apply_restore(other.id, plan["id"])
    for _ in range(31):
        service.preview_restore(chat.id, action="fork")
    with pytest.raises(ConflictError, match="capacity"):
        service.preview_restore(chat.id, action="fork")
    service.restoration.plans[plan["id"]]["created_at"] = 0
    with pytest.raises(ConflictError, match="expired"):
        service.apply_restore(chat.id, plan["id"])


def test_protected_files_untouched_and_excluded_child_blocks_deletion(workspace):
    service, chat, repo = workspace
    (repo / ".git").mkdir()
    (repo / ".git" / "index").write_bytes(b"index")
    (repo / ".env.local").write_bytes(b"private")
    (repo / "node_modules").mkdir()
    (repo / "node_modules" / "cached").write_bytes(b"cache")
    (repo / "code.py").write_bytes(b"edit")
    apply(service, chat, action="fork")
    assert (repo / ".git" / "index").read_bytes() == b"index"
    assert (repo / ".env.local").read_bytes() == b"private"
    assert (repo / "node_modules" / "cached").read_bytes() == b"cache"
    (repo / "nested").mkdir()
    (repo / "nested" / ".env").write_bytes(b"secret")
    tree = service.store.get_tree(chat.id)
    with pytest.raises(ConflictError, match="excluded"):
        service.preview_restore(chat.id, action="fork")
    assert service.store.get_tree(chat.id) == tree
    assert (repo / "nested" / ".env").read_bytes() == b"secret"


def test_hardlinks_rejected_before_mutation(workspace, tmp_path):
    service, chat, repo = workspace
    outside = tmp_path / "outside"
    outside.write_bytes(b"do not change")
    os.link(outside, repo / "alias")
    with pytest.raises((ValueError, PermissionError)):
        service.preview_restore(chat.id, action="fork")
    assert outside.read_bytes() == b"do not change"


def test_symlink_or_junction_rejected(workspace, tmp_path):
    service, chat, repo = workspace
    outside = tmp_path / "outside-dir"
    outside.mkdir()
    (outside / "safe").write_bytes(b"safe")
    alias = repo / "alias"
    if os.name == "nt":
        result = subprocess.run(
            ["cmd", "/c", "mklink", "/J", str(alias), str(outside)], capture_output=True
        )
        assert result.returncode == 0
    else:
        alias.symlink_to(outside, target_is_directory=True)
    with pytest.raises((ValueError, PermissionError, OSError)):
        service.preview_restore(chat.id, action="fork")
    assert (outside / "safe").read_bytes() == b"safe"


@pytest.mark.parametrize("key", ["../escape", "tree-../../escape", "tree-" + "0" * 64])
def test_snapshot_keys_and_missing_content_rejected(tmp_path, key):
    repository = SnapshotRepository(tmp_path / "snapshots")
    with pytest.raises((ValueError, FileNotFoundError)):
        repository.load(key)


@pytest.mark.parametrize("part", ["blob", "manifest"])
def test_corrupt_snapshot_stops_before_any_restore(workspace, part):
    service, chat, repo = workspace
    tree = service.store.get_tree(chat.id)
    baseline = tree.snapshots[0]
    simulated_turn(service, chat, repo, "change", b"new")
    plan = service.preview_restore(chat.id, action="fork")
    root = service.restoration.repository.root
    if part == "blob":
        path = root / "blobs" / baseline.manifest["entries"]["code.py"]["digest"]
    else:
        path = root / "trees" / baseline.storage_key
    path.write_bytes(b"corrupt")
    before = service.store.get_tree(chat.id)
    with pytest.raises(ValueError):
        service.apply_restore(chat.id, plan["id"])
    assert service.store.get_tree(chat.id) == before
    assert (repo / "code.py").read_bytes() == b"new"
    assert not service.restoration.pending(chat.id)


def test_snapshot_limits_and_concurrent_capture(workspace, monkeypatch):
    import src.workbench.files as module

    service, chat, repo = workspace
    monkeypatch.setattr(module, "MAX_FILE", 4)
    with pytest.raises(ValueError, match="32 MiB"):
        service.save_checkpoint(chat.id)
    monkeypatch.setattr(module, "MAX_FILE", 32 * 1024 * 1024)
    monkeypatch.setattr(module, "MAX_ENTRIES", 1)
    with pytest.raises(ValueError, match="entries"):
        service.save_checkpoint(chat.id)
    monkeypatch.setattr(module, "MAX_ENTRIES", 20000)
    monkeypatch.setattr(module, "MAX_TREE", 1)
    with pytest.raises(ValueError, match="128 MiB"):
        service.save_checkpoint(chat.id)
    monkeypatch.setattr(module, "MAX_TREE", 128 * 1024 * 1024)
    files = service.restoration.project(chat.id)[1]
    original = files.scan
    calls = 0

    def racing(sink=None):
        nonlocal calls
        calls += 1
        if calls == 2:
            (repo / "code.py").write_bytes(b"concurrent")
        return original(sink)

    monkeypatch.setattr(files, "scan", racing)
    with pytest.raises(ConflictError, match="during snapshot"):
        service.save_checkpoint(chat.id)


def test_readonly_file_mode_roundtrip(workspace):
    service, chat, repo = workspace
    path = repo / "code.py"
    path.chmod(stat.S_IREAD if os.name == "nt" else 0o444)
    saved = service.save_checkpoint(chat.id)
    path.chmod(stat.S_IWRITE | stat.S_IREAD if os.name == "nt" else 0o644)
    path.write_bytes(b"new")
    apply(service, chat, action="fork", node_id=saved.id)
    assert path.read_bytes() == b"base\n"
    assert not path.stat().st_mode & stat.S_IWUSR
    apply(service, chat, action="fork")
    assert path.stat().st_mode & stat.S_IWUSR


@pytest.mark.parametrize("point", ["before_step", "after_step", "before_commit", "after_commit"])
@pytest.mark.parametrize("decision", ["complete", "rollback"])
def test_failed_restore_reopen_and_explicit_recovery(tmp_path, point, decision):
    repo = tmp_path / "project"
    repo.mkdir()
    (repo / "a").write_bytes(b"base")
    (repo / "b").write_bytes(b"base")

    def fault(where, index):
        if where == point and index in (0, -1):
            raise RuntimeError("injected interruption")

    with service_at(tmp_path, restore_fault=fault) as service:
        chat = service.create_conversation(project_path=str(repo))
        (repo / "a").write_bytes(b"manual-a")
        (repo / "b").write_bytes(b"manual-b")
        original = service.store.get_tree(chat.id)
        plan = service.preview_restore(chat.id, action="fork")
        with pytest.raises(RuntimeError):
            service.apply_restore(chat.id, plan["id"])
        assert service.project_status(chat.id)["pending"]
        with pytest.raises(ConflictError):
            service.save_checkpoint(chat.id)
    with service_at(tmp_path) as reopened:
        if point in {"before_commit", "after_commit"}:
            # Coherent target plus durable apply intent proves completion. Startup
            # finishes the metadata/backup transaction without another user choice.
            assert not reopened.project_status(chat.id)["pending"]
            decision = "complete"
        restored = reopened.recover_restore(chat.id, plan["id"], decision=decision)
        assert restored["status"] == ("committed" if decision == "complete" else "rolled_back")
        assert not reopened.project_status(chat.id)["pending"]
        expected = b"base" if decision == "complete" else b"manual-a"
        assert (repo / "a").read_bytes() == expected
        assert not list(repo.glob(PRIVATE + "*"))
        if decision == "rollback":
            assert reopened.store.get_tree(chat.id) == original
        else:
            count = len(reopened.store.get_tree(chat.id).nodes)
            reopened.recover_restore(chat.id, plan["id"], decision=decision)
            assert len(reopened.store.get_tree(chat.id).nodes) == count


def test_unknown_edit_during_failure_blocks_recovery(workspace):
    service, chat, repo = workspace
    simulated_turn(service, chat, repo, "change", b"change")
    plan = service.preview_restore(chat.id, action="fork")

    def fault(where, index):
        if where == "after_step":
            raise RuntimeError()

    service.restoration.fault = fault
    with pytest.raises(RuntimeError):
        service.apply_restore(chat.id, plan["id"])
    (repo / "code.py").write_bytes(b"brand new user edit")
    with pytest.raises(ConflictError, match="new workspace"):
        service.recover_restore(chat.id, plan["id"], decision="complete")
    assert (repo / "code.py").read_bytes() == b"brand new user edit"
    assert service.project_status(chat.id)["pending"]


def test_recovery_can_itself_be_interrupted_and_resumed(tmp_path):
    repo = tmp_path / "project"
    repo.mkdir()
    for name in "abc":
        (repo / name).write_bytes(b"base")
    with service_at(tmp_path) as service:
        chat = service.create_conversation(project_path=str(repo))
        for name in "abc":
            (repo / name).write_bytes(b"manual")
        plan = service.preview_restore(chat.id, action="fork")

        def fault(where, index):
            if (where, index) in {("after_step", 0), ("recovery_before_step", 1)}:
                raise RuntimeError("interrupted")

        service.restoration.fault = fault
        with pytest.raises(RuntimeError):
            service.apply_restore(chat.id, plan["id"])
        with pytest.raises(RuntimeError):
            service.recover_restore(chat.id, plan["id"], decision="complete")
    with service_at(tmp_path) as service:
        service.recover_restore(chat.id, plan["id"], decision="complete")
        assert all((repo / name).read_bytes() == b"base" for name in "abc")
        assert not service.project_status(chat.id)["pending"]


@pytest.mark.parametrize("decision", ["complete", "rollback"])
def test_failure_after_source_moved_before_publication(workspace, monkeypatch, decision):
    service, chat, repo = workspace
    simulated_turn(service, chat, repo, "change", b"changed")
    plan = service.preview_restore(chat.id, action="fork")
    files = service.restoration.project(chat.id)[1]
    if files.broker:
        original = files.broker._rename

        def interrupted(handle, parent, name, **kwargs):
            result = original(handle, parent, name, **kwargs)
            if name.startswith(PRIVATE) and "stage" not in name:
                raise RuntimeError("source moved")
            return result

        monkeypatch.setattr(files.broker, "_rename", interrupted)
    else:
        original = files._rename

        def interrupted(parent, source, target, destination=None):
            result = original(parent, source, target, destination)
            if target.startswith(PRIVATE):
                raise RuntimeError("source moved")
            return result

        monkeypatch.setattr(files, "_rename", interrupted)
    with pytest.raises(RuntimeError):
        service.apply_restore(chat.id, plan["id"])
    assert not (repo / "code.py").exists()
    monkeypatch.undo()
    service.recover_restore(chat.id, plan["id"], decision=decision)
    assert (repo / "code.py").read_bytes() == (b"base\n" if decision == "complete" else b"changed")
    assert not list(repo.glob(PRIVATE + "*"))


def test_reopened_service_holds_registered_project_lock(tmp_path):
    repo = tmp_path / "project"
    repo.mkdir()
    with service_at(tmp_path) as service:
        service.create_conversation(project_path=str(repo))
    with service_at(tmp_path):
        with WorkbenchService(tmp_path / "state2", project_lock_dir=tmp_path / "locks") as other:
            with pytest.raises(ConflictError):
                other.create_conversation(project_path=str(repo))


def test_new_editor_file_after_source_rename_is_preserved(workspace, monkeypatch):
    service, chat, repo = workspace
    simulated_turn(service, chat, repo, "change", b"changed")
    plan = service.preview_restore(chat.id, action="fork")
    files = service.restoration.project(chat.id)[1]
    if not files.broker:
        pytest.skip("Windows native rename hook")
    original = files.broker._rename

    def racing(handle, parent, name, **kwargs):
        result = original(handle, parent, name, **kwargs)
        if name.startswith(PRIVATE):
            (repo / "code.py").write_bytes(b"external editor")
        return result

    monkeypatch.setattr(files.broker, "_rename", racing)
    with pytest.raises(OSError):
        service.apply_restore(chat.id, plan["id"])
    monkeypatch.undo()
    assert (repo / "code.py").read_bytes() == b"external editor"
    with pytest.raises(ConflictError):
        service.recover_restore(chat.id, plan["id"], decision="complete")
    assert (repo / "code.py").read_bytes() == b"external editor"


def test_edit_after_recovery_scan_before_read_must_not_be_overwritten(workspace, monkeypatch):
    service, chat, repo = workspace
    simulated_turn(service, chat, repo, "change", b"changed")
    plan = service.preview_restore(chat.id, action="fork")

    def fault(where, index):
        if where == "before_step":
            raise RuntimeError()

    service.restoration.fault = fault
    with pytest.raises(RuntimeError):
        service.apply_restore(chat.id, plan["id"])
    service.restoration.fault = lambda *_: None
    files = service.restoration.project(chat.id, recovery=True)[1]
    original = files.read
    known_live = service.restoration._known_live
    armed = False

    def arm(plan, files):
        nonlocal armed
        live = known_live(plan, files)
        armed = "parent" in plan
        return live

    def racing(path):
        nonlocal armed
        if armed:
            armed = False
            (repo / "code.py").write_bytes(b"new concurrent edit")
        return original(path)

    monkeypatch.setattr(files, "read", racing)
    monkeypatch.setattr(service.restoration, "_known_live", arm)
    with pytest.raises(ConflictError):
        service.recover_restore(chat.id, plan["id"], decision="complete")
    assert (repo / "code.py").read_bytes() == b"new concurrent edit"


def test_file_edit_between_plan_validation_and_write_is_preserved(workspace):
    service, chat, repo = workspace
    simulated_turn(service, chat, repo, "change", b"change")
    plan = service.preview_restore(chat.id, action="fork")

    def fault(where, index):
        if where == "before_step":
            (repo / "code.py").write_bytes(b"racing edit")

    service.restoration.fault = fault
    with pytest.raises(ConflictError):
        service.apply_restore(chat.id, plan["id"])
    assert (repo / "code.py").read_bytes() == b"racing edit"


def test_restore_crash_subprocess_releases_lock_without_startup_writes(tmp_path):
    repo = tmp_path / "project"
    repo.mkdir()
    (repo / "a").write_bytes(b"base")
    script = """
import os, sys
from pathlib import Path
from tests.workbench_unit_service import WorkbenchService
p=Path(sys.argv[1])
def fault(where,index):
    if where=='after_step': os._exit(73)
s=WorkbenchService(p/'state',project_lock_dir=p/'locks',restore_fault=fault)
c=s.create_conversation(project_path=str(p/'project'))
(p/'project'/'a').write_bytes(b'manual')
plan=s.preview_restore(c.id,action='fork')
s.apply_restore(c.id,plan['id'])
"""
    child = subprocess.run(
        [sys.executable, "-c", script, str(tmp_path)], capture_output=True, timeout=15
    )
    assert child.returncode == 73, child.stderr.decode()
    before = (repo / "a").read_bytes()
    with service_at(tmp_path) as service:
        chat = service.store.list_conversations()[0]
        assert (repo / "a").read_bytes() == before
        assert not service.project_status(chat.id)["pending"]
        assert (repo / "a").read_bytes() == b"base"


def test_project_lock_across_states_nested_roots_and_duplicate_conversations(tmp_path):
    repo = tmp_path / "project"
    repo.mkdir()
    nested = repo / "nested"
    nested.mkdir()
    with service_at(tmp_path) as first:
        first.create_conversation(project_path=str(repo))
        assert first.create_conversation(project_path=str(repo)).base_snapshot_id
        with WorkbenchService(tmp_path / "state2", project_lock_dir=tmp_path / "locks") as second:
            for target in (repo, nested, tmp_path):
                with pytest.raises((ConflictError, ValueError)):
                    second.create_conversation(project_path=str(target))
    with WorkbenchService(tmp_path / "state2", project_lock_dir=tmp_path / "locks") as second:
        assert second.create_conversation(project_path=str(repo)).base_snapshot_id


def test_disjoint_project_storage_and_execution_gate(tmp_path):
    with service_at(tmp_path) as service:
        with pytest.raises(ValueError, match="disjoint"):
            service.create_conversation(project_path=str(tmp_path))
        repo = tmp_path / "repo"
        repo.mkdir()
        chat = service.create_conversation(project_path=str(repo))
        before = service.store.get_tree(chat.id)
        for operation in (
            lambda: service.fork(chat.id, None),
            lambda: service.switch_branch(chat.id, chat.active_branch_id),
        ):
            with pytest.raises(UnsupportedModeError):
                operation()
        assert service.store.get_tree(chat.id) == before


def test_http_project_preview_checkpoint_apply_recovery(tmp_path):
    from src.workbench.http import WorkbenchHTTPServer
    from tests.test_workbench_http import TOKEN

    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "a").write_bytes(b"base")
    with service_at(tmp_path) as service:
        with WorkbenchHTTPServer(("127.0.0.1", 0), service, token=TOKEN) as server:
            thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.02})
            thread.start()
            try:
                status, chat = request(
                    server, "POST", "/api/conversations", {"project_path": str(repo)}
                )
                assert status == 201 and chat["base_snapshot_id"]
                base = "/api/conversations/" + chat["id"]
                (repo / "a").write_bytes(b"manual")
                assert request(server, "GET", base + "/workspace")[1]["manual_changes"]
                assert (
                    request(
                        server,
                        "POST",
                        base + "/restores/preview",
                        {"action": "fork"},
                        headers={"Authorization": "Bearer wrong"},
                    )[0]
                    == 401
                )
                status, preview = request(
                    server, "POST", base + "/restores/preview", {"action": "fork"}
                )
                assert status == 201
                assert (
                    request(server, "POST", base + "/restores/apply", {"plan_id": preview["id"]})[0]
                    == 200
                )
                assert (repo / "a").read_bytes() == b"base"
                assert (
                    request(
                        server,
                        "POST",
                        base + "/restores/recover",
                        {"operation_id": preview["id"], "decision": "complete"},
                    )[0]
                    == 200
                )
                assert request(server, "POST", base + "/checkpoints", {})[0] == 201
            finally:
                server.shutdown()
                thread.join(3)


def test_store_atomic_restore_commit_rolls_back_all_history_on_invalid_destination(workspace):
    service, chat, repo = workspace
    (repo / "code.py").write_bytes(b"manual")
    preview = service.preview_restore(chat.id, action="fork")
    plan = {**service.restoration.plans[preview["id"]], "destination_node": "node-" + "0" * 32}
    tree = service.store.get_tree(chat.id)
    with pytest.raises(NotFoundError):
        service.store.commit_restore(plan)
    assert service.store.get_tree(chat.id) == tree


def test_busy_project_blocks_checkpoint_and_preview(workspace):
    service, chat, _ = workspace
    turn = service.store.append_turn(chat.id, "pending", mode="repair")
    tree = service.store.get_tree(chat.id)
    for operation in (
        lambda: service.save_checkpoint(chat.id),
        lambda: service.preview_restore(chat.id, action="fork"),
    ):
        with pytest.raises(ConflictError):
            operation()
    assert service.store.get_tree(chat.id) == tree
    service.store.finish_run(chat.id, turn.run.id, status="cancelled")


def test_same_branch_restore_preserves_manual_checkpoint_on_original(workspace):
    service, chat, repo = workspace
    (repo / "code.py").write_bytes(b"manual")
    apply(service, chat, action="switch", branch_id=chat.active_branch_id)
    assert (repo / "code.py").read_bytes() == b"base\n"
    tree = service.store.get_tree(chat.id)
    assert len(tree.branches) == 2
    original = next(b for b in tree.branches if b.id == chat.active_branch_id)
    manual = next(n for n in tree.nodes if n.id == original.head_node_id)
    assert manual.user_text == "本地修改"
    apply(service, chat, action="switch", branch_id=chat.active_branch_id)
    assert (repo / "code.py").read_bytes() == b"manual"


def test_new_junction_between_preflight_and_apply_cannot_redirect_write(workspace, tmp_path):
    service, chat, repo = workspace
    (repo / "folder").mkdir()
    (repo / "folder" / "a").write_bytes(b"original")
    saved = service.save_checkpoint(chat.id)
    (repo / "folder" / "a").write_bytes(b"manual")
    plan = service.preview_restore(chat.id, action="fork", node_id=saved.id)
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "a").write_bytes(b"outside")

    def racing(where, index):
        if where != "before_step":
            return
        (repo / "folder").rename(repo / "old-folder")
        if os.name == "nt":
            subprocess.run(
                ["cmd", "/c", "mklink", "/J", str(repo / "folder"), str(outside)],
                check=True,
                capture_output=True,
            )
        else:
            (repo / "folder").symlink_to(outside, target_is_directory=True)

    service.restoration.fault = racing
    with pytest.raises((PermissionError, OSError, ConflictError)):
        service.apply_restore(chat.id, plan["id"])
    assert (outside / "a").read_bytes() == b"outside"
    assert (repo / "old-folder" / "a").read_bytes() == b"manual"


def test_root_alias_rejected_and_root_pinned(workspace, tmp_path):
    service, chat, repo = workspace
    if os.name != "nt":
        pytest.skip("Windows root pinning")
    with pytest.raises(OSError):
        repo.rename(tmp_path / "renamed")
    alias = tmp_path / "alias-root"
    subprocess.run(
        ["cmd", "/c", "mklink", "/J", str(alias), str(repo)], check=True, capture_output=True
    )
    with pytest.raises(ValueError, match="links"):
        service.create_conversation(project_path=str(alias))


@pytest.mark.parametrize("unsafe", [".git/x", "../x", "a:b", "a\\b", "CON.txt", "x.", "x "])
def test_manifests_reject_unsafe_paths(workspace, unsafe):
    service, chat, _ = workspace
    baseline = service.store.get_tree(chat.id).snapshots[0].manifest
    manifest = {**baseline, "entries": {unsafe: {"kind": "directory", "mode": 0o755}}}
    with pytest.raises((ValueError, PermissionError)):
        service.restoration.repository.validate(manifest)


def test_manifest_missing_parent_size_and_case_aliases_rejected(workspace):
    service, chat, _ = workspace
    manifest = service.store.get_tree(chat.id).snapshots[0].manifest
    entry = manifest["entries"]["code.py"]
    for entries in (
        {"missing/a": entry},
        {"code.py": {**entry, "size": 100}},
        {"code.py": entry, "Code.py": entry},
    ):
        with pytest.raises(ValueError):
            service.restoration.repository.validate({**manifest, "entries": entries})
