"""S1 acceptance: immutable branches, durable history, and scoped state references."""

import json
import os
import sqlite3
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Barrier

import pytest

from src.workbench import ConflictError, NotFoundError, SchemaError, WorkbenchStore
from src.workbench.schema import APPLICATION_ID, SCHEMA_VERSION


@pytest.fixture
def store(tmp_path):
    return WorkbenchStore(tmp_path / "state" / "workbench.sqlite3")


@pytest.fixture
def chat(store):
    return store.create_conversation("日常问答")


@pytest.fixture
def project(store, tmp_path):
    return store.create_conversation("修复项目", project_path=str(tmp_path / "repo"))


def completed_turn(store, conversation_id, text, answer="回答", **kwargs):
    turn = store.append_turn(conversation_id, text, **kwargs)
    store.start_run(conversation_id, turn.run.id)
    store.finish_run(conversation_id, turn.run.id, answer=answer)
    return turn


def test_create_list_rename_and_reopen(store, chat, project):
    assert chat.project_path is None
    assert Path(project.project_path).is_absolute()
    assert store.get_path(chat.id, None) == ()
    tree = store.get_tree(chat.id)
    assert tree.nodes == tree.runs == tree.snapshots == ()
    assert len(tree.branches) == 1
    assert tree.branches[0].name == "main"
    assert tree.branches[0].head_node_id is None
    renamed = store.rename_conversation(chat.id, "写作", expected_revision=chat.revision)
    assert renamed.revision == 1
    assert renamed.title == "写作"
    assert store.list_conversations()[0] == renamed
    reopened = WorkbenchStore(store.database)
    assert reopened.get_tree(chat.id) == store.get_tree(chat.id)
    assert reopened.list_conversations() == store.list_conversations()


def test_edit_first_turn_preserves_original_future(store, chat):
    original_branch = chat.active_branch_id
    first = completed_turn(store, chat.id, "对话1", "原回答1")
    second = completed_turn(store, chat.id, "对话2", "原回答2")
    third = completed_turn(store, chat.id, "对话3", "原回答3")
    before = store.get_tree(chat.id)

    edited = store.edit_turn(chat.id, first.node.id, "新对话1")
    assert edited.node.parent_id is None
    assert edited.node.id != first.node.id
    assert edited.run.status == "queued"
    assert edited.run.id != first.run.id
    new_branch = store.get_conversation(chat.id).active_branch_id
    assert new_branch != original_branch
    assert store.get_messages(chat.id, edited.node.id) == ({"role": "user", "content": "新对话1"},)
    assert store.get_branch_path(chat.id, original_branch) == (
        first.node,
        second.node,
        third.node,
    )
    after = store.get_tree(chat.id)
    assert set(before.nodes).issubset(set(after.nodes))
    assert [run for run in after.runs if run.id in {r.id for r in before.runs}] == list(before.runs)
    store.start_run(chat.id, edited.run.id)
    store.finish_run(chat.id, edited.run.id, answer="新回答1")
    followup = completed_turn(store, chat.id, "新对话2", "新回答2")
    assert followup.node.parent_id == edited.node.id
    assert [m["content"] for m in store.get_messages(chat.id, followup.node.id)] == [
        "新对话1",
        "新回答1",
        "新对话2",
        "新回答2",
    ]
    store.switch_branch(chat.id, original_branch)
    resumed = completed_turn(store, chat.id, "原分支对话4")
    assert resumed.node.parent_id == third.node.id
    assert [n.id for n in store.get_branch_path(chat.id, new_branch)] == [
        edited.node.id,
        followup.node.id,
    ]


def test_edit_middle_turn_inherits_only_ancestors(store, chat):
    first = completed_turn(store, chat.id, "共同问题", "共同回答")
    middle = completed_turn(store, chat.id, "旧方案", "旧结果")
    completed_turn(store, chat.id, "依赖旧方案的追问", "不应进入新分支")
    edited = store.edit_turn(chat.id, middle.node.id, "新方案")
    assert edited.node.parent_id == first.node.id
    assert [m["content"] for m in store.get_messages(chat.id, edited.node.id)] == [
        "共同问题",
        "共同回答",
        "新方案",
    ]
    assert len(store.get_tree(chat.id).nodes) == 4


def test_regenerate_is_sibling_with_same_request_and_mode(store, project):
    first = completed_turn(store, project.id, "分析", mode="project_chat")
    target = completed_turn(store, project.id, "修复它", "原补丁", mode="repair")
    regenerated = store.regenerate_turn(project.id, target.node.id)
    assert regenerated.node.parent_id == first.node.id
    assert regenerated.node.user_text == target.node.user_text
    assert regenerated.node.mode == "repair"
    assert regenerated.run.answer == ""
    assert store.get_run(project.id, target.run.id).answer == "原补丁"


def test_browsing_does_not_change_append_location(store, chat):
    first = completed_turn(store, chat.id, "1")
    second = completed_turn(store, chat.id, "2")
    selected = store.view_node(chat.id, first.node.id)
    assert selected.view_node_id == first.node.id
    assert selected.active_branch_id == chat.active_branch_id
    reopened = WorkbenchStore(store.database)
    assert reopened.get_conversation(chat.id) == selected
    third = completed_turn(reopened, chat.id, "3")
    assert third.node.parent_id == second.node.id
    selected = reopened.view_node(chat.id, None)
    assert selected.view_node_id is None
    assert selected.active_branch_id == chat.active_branch_id


def test_multi_step_return_fork_and_switch(store, chat):
    turns = [completed_turn(store, chat.id, str(i)) for i in range(5)]
    for turn in reversed(turns):
        store.view_node(chat.id, turn.node.id)
    new_branch = store.fork_at(chat.id, turns[0].node.id, name="从第一轮继续")
    assert new_branch.fork_node_id == new_branch.head_node_id == turns[0].node.id
    assert store.get_branch_path(chat.id, new_branch.id) == (turns[0].node,)
    sibling = completed_turn(store, chat.id, "新的第二轮")
    assert sibling.node.parent_id == turns[0].node.id
    store.switch_branch(chat.id, chat.active_branch_id)
    assert store.get_conversation(chat.id).view_node_id == turns[-1].node.id
    root = store.fork_at(chat.id, None, name="从空历史继续")
    assert store.get_branch_path(chat.id, root.id) == ()
    assert len(store.get_tree(chat.id).nodes) == 6


def test_project_snapshot_lineage_and_manual_checkpoint(store, project):
    initial = store.record_snapshot(project.id, "blob/S0", as_baseline=True)
    assert store.get_conversation(project.id).base_snapshot_id == initial.id
    assert store.get_effective_snapshot(project.id, None) == initial.id
    first = completed_turn(store, project.id, "对话1", mode="project_chat")
    second = completed_turn(store, project.id, "对话2", mode="project_chat")
    third = store.append_turn(project.id, "对话3", mode="repair")
    assert first.node.input_snapshot_id == second.node.input_snapshot_id == initial.id
    assert third.node.input_snapshot_id == initial.id
    store.start_run(project.id, third.run.id)
    modified = store.record_snapshot(project.id, "blob/S3", manifest={"files": ["a.py"]})
    result = store.finish_run(
        project.id,
        third.run.id,
        answer="已修复",
        output_snapshot_id=modified.id,
        artifacts={"diff": "runs/3/patch.diff", "verification": {"status": "passed"}},
    )
    assert result.output_snapshot_id == modified.id
    assert store.get_effective_snapshot(project.id, third.node.id) == modified.id

    manual = store.record_snapshot(project.id, "blob/manual", manifest={"files": ["a.py", "b.py"]})
    checkpoint = store.append_local_checkpoint(project.id, manual.id)
    assert checkpoint.parent_id == third.node.id
    assert checkpoint.kind == "local_checkpoint"
    assert checkpoint.input_snapshot_id == modified.id
    assert store.get_effective_snapshot(project.id, checkpoint.id) == manual.id
    assert len(store.get_messages(project.id, checkpoint.id)) == 6
    original = store.get_tree(project.id)
    edited = store.edit_turn(project.id, first.node.id, "新对话1")
    assert edited.node.input_snapshot_id == initial.id
    assert store.get_effective_snapshot(project.id, edited.node.id) == initial.id
    store.finish_run(project.id, edited.run.id, status="cancelled")
    store.switch_branch(project.id, project.active_branch_id)
    assert store.get_conversation(project.id).view_node_id == checkpoint.id
    followup = store.append_turn(project.id, "基于手动修改继续", mode="repair")
    assert followup.node.input_snapshot_id == manual.id
    assert set(s.id for s in original.snapshots) <= set(
        s.id for s in store.get_tree(project.id).snapshots
    )


def test_regenerate_preserves_explicit_pre_execution_snapshot(store, project):
    baseline = store.record_snapshot(project.id, "baseline", as_baseline=True)
    first = completed_turn(store, project.id, "问题", mode="project_chat")
    pre = store.record_snapshot(project.id, "pre")
    turn = store.append_turn(project.id, "修改", mode="repair", input_snapshot_id=pre.id)
    store.start_run(project.id, turn.run.id)
    post = store.record_snapshot(project.id, "post")
    store.finish_run(project.id, turn.run.id, output_snapshot_id=post.id)
    repeated = store.regenerate_turn(project.id, turn.node.id)
    assert repeated.node.parent_id == first.node.id
    assert repeated.node.input_snapshot_id == pre.id
    assert repeated.node.input_snapshot_id != baseline.id


@pytest.mark.parametrize("status", ["succeeded", "failed", "cancelled", "interrupted"])
def test_run_lifecycle_and_terminal_result_are_persistent(store, project, status):
    turn = store.append_turn(project.id, "请求", mode="repair")
    assert turn.run.status == "queued"
    assert turn.run.started_at is turn.run.finished_at is None
    running = store.start_run(project.id, turn.run.id)
    assert running.status == "running"
    assert running.started_at is not None
    snapshot = store.record_snapshot(project.id, "output")
    final = store.finish_run(
        project.id,
        turn.run.id,
        status=status,
        answer="执行结论",
        error=None if status == "succeeded" else "失败原因",
        artifacts={"trace": "trace.jsonl"},
        output_snapshot_id=snapshot.id,
    )
    assert final.finished_at >= final.started_at
    assert WorkbenchStore(store.database).get_run(project.id, turn.run.id) == final
    assert store.get_effective_snapshot(project.id, turn.node.id) == snapshot.id
    with pytest.raises(ConflictError):
        store.finish_run(project.id, turn.run.id, answer="覆盖旧结果")
    with pytest.raises(ConflictError):
        store.start_run(project.id, turn.run.id)
    assert store.get_run(project.id, turn.run.id) == final


@pytest.mark.parametrize("status", ["queued", "running"])
def test_unfinished_turn_blocks_history_mutations_but_allows_browsing(store, chat, status):
    first = completed_turn(store, chat.id, "完成的轮次")
    pending = store.append_turn(chat.id, "未完成")
    if status == "running":
        store.start_run(chat.id, pending.run.id)
    operations = [
        lambda: store.append_turn(chat.id, "另一请求"),
        lambda: store.fork_at(chat.id, first.node.id),
        lambda: store.edit_turn(chat.id, first.node.id, "编辑"),
        lambda: store.regenerate_turn(chat.id, first.node.id),
        lambda: store.switch_branch(chat.id, chat.active_branch_id),
    ]
    for operation in operations:
        before = store.get_tree(chat.id)
        with pytest.raises(ConflictError):
            operation()
        assert store.get_tree(chat.id) == before
    store.view_node(chat.id, first.node.id)
    store.finish_run(chat.id, pending.run.id, status="cancelled")
    assert store.fork_at(chat.id, first.node.id).head_node_id == first.node.id


def test_cancel_queued_but_cannot_claim_it_executed(store, chat):
    turn = store.append_turn(chat.id, "还未执行")
    for status in ["succeeded", "failed"]:
        with pytest.raises(ConflictError):
            store.finish_run(chat.id, turn.run.id, status=status)
    cancelled = store.finish_run(chat.id, turn.run.id, status="cancelled")
    assert cancelled.started_at is None
    assert cancelled.finished_at is not None


@pytest.mark.parametrize("started", [False, True])
def test_explicit_restart_recovery_does_not_replay_or_interrupt_on_open(store, chat, started):
    previous = completed_turn(store, chat.id, "已完成")
    pending = store.append_turn(chat.id, "中断请求")
    if started:
        store.start_run(chat.id, pending.run.id)
    before = store.get_tree(chat.id)
    reopened = WorkbenchStore(store.database)
    assert reopened.get_tree(chat.id) == before
    recovered = reopened.interrupt_pending_runs(chat.id)
    assert len(recovered) == 1
    assert recovered[0].id == pending.run.id
    assert recovered[0].status == "interrupted"
    assert recovered[0].answer == ""
    assert reopened.get_run(chat.id, previous.run.id) == store.get_run(chat.id, previous.run.id)
    assert reopened.interrupt_pending_runs(chat.id) == ()
    repeated = reopened.regenerate_turn(chat.id, pending.node.id)
    assert repeated.node.parent_id == previous.node.id
    assert len(reopened.get_tree(chat.id).runs) == 3


def test_changes_survive_fresh_python_process(store, project):
    snapshot = store.record_snapshot(project.id, "baseline", as_baseline=True)
    first = completed_turn(store, project.id, "你好", "回答", mode="project_chat")
    store.regenerate_turn(project.id, first.node.id)
    code = """
import json, sys
from dataclasses import asdict
from src.workbench import WorkbenchStore
store = WorkbenchStore(sys.argv[1])
print(json.dumps(asdict(store.get_tree(sys.argv[2])), ensure_ascii=False))
"""
    result = subprocess.run(
        [sys.executable, "-c", code, str(store.database), project.id],
        cwd=Path(__file__).resolve().parents[1],
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=30,
        check=True,
        env={**os.environ, "PYTHONIOENCODING": "utf-8"},
    )
    data = json.loads(result.stdout)
    assert data["conversation"]["base_snapshot_id"] == snapshot.id
    assert len(data["branches"]) == 2
    assert len(data["nodes"]) == len(data["runs"]) == 2
    assert data["conversation"]["active_branch_id"] != project.active_branch_id
    assert [r["status"] for r in data["runs"]] == ["succeeded", "queued"]


def test_objects_from_another_conversation_are_rejected_atomically(store, project, tmp_path):
    other = store.create_conversation("另一个", project_path=str(tmp_path / "other"))
    node = completed_turn(store, other.id, "另一个项目")
    snapshot = store.record_snapshot(other.id, "other-snapshot")
    before = store.get_tree(project.id)
    operations = [
        lambda: store.view_node(project.id, node.node.id),
        lambda: store.fork_at(project.id, node.node.id),
        lambda: store.edit_turn(project.id, node.node.id, "编辑"),
        lambda: store.regenerate_turn(project.id, node.node.id),
        lambda: store.get_path(project.id, node.node.id),
        lambda: store.get_messages(project.id, node.node.id),
        lambda: store.get_effective_snapshot(project.id, node.node.id),
        lambda: store.get_branch_path(project.id, other.active_branch_id),
        lambda: store.switch_branch(project.id, other.active_branch_id),
        lambda: store.append_turn(project.id, "请求", input_snapshot_id=snapshot.id),
        lambda: store.append_local_checkpoint(project.id, snapshot.id),
        lambda: store.get_run(project.id, node.run.id),
        lambda: store.start_run(project.id, node.run.id),
        lambda: store.finish_run(project.id, node.run.id),
    ]
    for operation in operations:
        with pytest.raises(NotFoundError):
            operation()
        assert store.get_tree(project.id) == before


def test_foreign_output_snapshot_does_not_finish_run(store, project, tmp_path):
    other = store.create_conversation(project_path=str(tmp_path / "other"))
    snapshot = store.record_snapshot(other.id, "foreign")
    pending = store.append_turn(project.id, "修改")
    running = store.start_run(project.id, pending.run.id)
    with pytest.raises(NotFoundError):
        store.finish_run(project.id, pending.run.id, output_snapshot_id=snapshot.id)
    assert store.get_run(project.id, pending.run.id) == running


@pytest.mark.parametrize("mode", ["project_chat", "repair", "unknown"])
def test_project_modes_require_project_and_unknown_mode_is_rejected(store, chat, mode):
    before = store.get_tree(chat.id)
    with pytest.raises(ValueError):
        store.append_turn(chat.id, "请求", mode=mode)
    assert store.get_tree(chat.id) == before


def test_chat_can_be_used_in_project_conversation_without_file_access(store, project):
    assert not Path(project.project_path).exists()
    completed_turn(store, project.id, "帮我写一首诗", "诗")
    assert not Path(project.project_path).exists()
    with pytest.raises(ValueError):
        store.record_snapshot(store.create_conversation().id, "snapshot")


@pytest.mark.parametrize("bad_text", ["", " \n ", None, 1])
def test_invalid_edit_rolls_back_branch_creation(store, chat, bad_text):
    turn = completed_turn(store, chat.id, "原请求")
    before = store.get_tree(chat.id)
    with pytest.raises(ValueError):
        store.edit_turn(chat.id, turn.node.id, bad_text)
    assert store.get_tree(chat.id) == before


def test_invalid_branch_name_rolls_back(store, chat):
    turn = completed_turn(store, chat.id, "原请求")
    before = store.get_tree(chat.id)
    with pytest.raises(ValueError):
        store.regenerate_turn(chat.id, turn.node.id, name="")
    assert store.get_tree(chat.id) == before


def test_stale_revision_rejects_without_changing_history(store, chat):
    first = completed_turn(store, chat.id, "第一轮")
    revision = store.get_conversation(chat.id).revision
    store.view_node(chat.id, None, expected_revision=revision)
    before = store.get_tree(chat.id)
    operations = [
        lambda: store.append_turn(chat.id, "请求", expected_revision=revision),
        lambda: store.edit_turn(chat.id, first.node.id, "编辑", expected_revision=revision),
        lambda: store.regenerate_turn(chat.id, first.node.id, expected_revision=revision),
        lambda: store.fork_at(chat.id, None, expected_revision=revision),
        lambda: store.view_node(chat.id, first.node.id, expected_revision=revision),
        lambda: store.switch_branch(chat.id, chat.active_branch_id, expected_revision=revision),
        lambda: store.rename_conversation(chat.id, "标题", expected_revision=revision),
    ]
    for operation in operations:
        with pytest.raises(ConflictError, match="revision"):
            operation()
        assert store.get_tree(chat.id) == before


def test_concurrent_edits_have_one_winner_and_no_orphans(store, chat):
    original = completed_turn(store, chat.id, "原请求")
    revision = store.get_conversation(chat.id).revision
    barrier = Barrier(2)

    def edit(text):
        connection = WorkbenchStore(store.database)
        barrier.wait(timeout=10)
        try:
            return connection.edit_turn(chat.id, original.node.id, text, expected_revision=revision)
        except ConflictError:
            return None

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(edit, ["编辑A", "编辑B"]))
    assert sum(result is not None for result in results) == 1
    tree = store.get_tree(chat.id)
    assert len(tree.branches) == len(tree.nodes) == len(tree.runs) == 2
    assert tree.conversation.revision == revision + 1
    assert store.get_branch_path(chat.id, chat.active_branch_id) == (original.node,)


def test_concurrent_run_start_is_claimed_once(store, chat):
    turn = store.append_turn(chat.id, "请求")
    barrier = Barrier(2)

    def start(_):
        connection = WorkbenchStore(store.database)
        barrier.wait(timeout=10)
        try:
            return connection.start_run(chat.id, turn.run.id)
        except ConflictError:
            return None

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(start, range(2)))
    assert sum(result is not None for result in results) == 1
    assert store.get_run(chat.id, turn.run.id).status == "running"


def test_baseline_cannot_be_replaced_or_added_after_history(store, project):
    baseline = store.record_snapshot(project.id, "base", as_baseline=True)
    before = store.get_tree(project.id)
    with pytest.raises(ConflictError):
        store.record_snapshot(project.id, "replacement", as_baseline=True)
    assert store.get_tree(project.id) == before
    assert store.get_effective_snapshot(project.id, None) == baseline.id
    other = store.create_conversation(project_path=project.project_path)
    completed_turn(store, other.id, "已有历史")
    before = store.get_tree(other.id)
    with pytest.raises(ConflictError):
        store.record_snapshot(other.id, "late", as_baseline=True)
    assert store.get_tree(other.id) == before


def test_local_checkpoint_cannot_be_edited_and_is_inherited(store, project):
    snap = store.record_snapshot(project.id, "manual")
    checkpoint = store.append_local_checkpoint(project.id, snap.id)
    before = store.get_tree(project.id)
    with pytest.raises(ValueError):
        store.edit_turn(project.id, checkpoint.id, "编辑")
    with pytest.raises(ValueError):
        store.regenerate_turn(project.id, checkpoint.id)
    assert store.get_tree(project.id) == before
    assert store.get_messages(project.id, checkpoint.id) == ()
    turn = completed_turn(store, project.id, "下一请求")
    assert turn.node.input_snapshot_id == snap.id
    assert turn.node.parent_id == checkpoint.id
    assert len(store.get_messages(project.id, turn.node.id)) == 2


def test_pending_execution_blocks_local_checkpoint(store, project):
    snap = store.record_snapshot(project.id, "manual")
    store.append_turn(project.id, "执行中")
    before = store.get_tree(project.id)
    with pytest.raises(ConflictError):
        store.append_local_checkpoint(project.id, snap.id)
    assert store.get_tree(project.id) == before


@pytest.mark.parametrize("metadata", [["list"], {"n": float("nan")}, {"n": object()}])
def test_invalid_json_metadata_does_not_write(store, project, metadata):
    before = store.get_tree(project.id)
    with pytest.raises((TypeError, ValueError)):
        store.record_snapshot(project.id, "snapshot", manifest=metadata)
    assert store.get_tree(project.id) == before
    turn = store.append_turn(project.id, "请求")
    running = store.start_run(project.id, turn.run.id)
    with pytest.raises((TypeError, ValueError)):
        store.finish_run(project.id, turn.run.id, artifacts=metadata)
    assert store.get_run(project.id, turn.run.id) == running


def test_snapshot_and_artifact_values_are_detached_from_callers(store, project):
    metadata = {"files": ["a.py"]}
    snap = store.record_snapshot(project.id, "snapshot", manifest=metadata)
    metadata["files"].append("changed.py")
    snap.manifest["files"].append("local-only.py")
    assert store.get_tree(project.id).snapshots[0].manifest == {"files": ["a.py"]}
    turn = store.append_turn(project.id, "请求")
    store.start_run(project.id, turn.run.id)
    result = store.finish_run(project.id, turn.run.id, artifacts={"paths": ["diff"]})
    result.artifacts["paths"].append("not-persisted")
    assert store.get_run(project.id, turn.run.id).artifacts == {"paths": ["diff"]}


def test_missing_objects_and_empty_database_paths(store):
    with pytest.raises(NotFoundError):
        store.get_conversation("missing")
    with pytest.raises(NotFoundError):
        store.get_tree("missing")
    with pytest.raises(NotFoundError):
        store.get_path("missing", None)
    with pytest.raises(ValueError):
        WorkbenchStore(":memory:")


@pytest.mark.parametrize("version,application", [(99, APPLICATION_ID), (1, 999), (0, 999)])
def test_unsupported_database_is_rejected_without_migration(tmp_path, version, application):
    path = tmp_path / "unsupported.sqlite3"
    with sqlite3.connect(path) as db:
        db.execute(f"PRAGMA user_version = {version}")
        db.execute(f"PRAGMA application_id = {application}")
    with pytest.raises(SchemaError):
        WorkbenchStore(path)
    with sqlite3.connect(path) as db:
        assert db.execute("PRAGMA user_version").fetchone()[0] == version
        assert db.execute("PRAGMA application_id").fetchone()[0] == application
        assert db.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall() == []


def test_existing_unrelated_database_is_not_overwritten(tmp_path):
    path = tmp_path / "foreign.sqlite3"
    with sqlite3.connect(path) as db:
        db.execute("CREATE TABLE unrelated (value TEXT)")
        db.execute("INSERT INTO unrelated VALUES ('preserve')")
    with pytest.raises(SchemaError):
        WorkbenchStore(path)
    with sqlite3.connect(path) as db:
        assert db.execute("SELECT * FROM unrelated").fetchall() == [("preserve",)]
        assert db.execute("PRAGMA journal_mode").fetchone()[0] == "delete"


def test_sqlite_constraints_enforce_scoped_references_and_immutable_history(store, project):
    first = completed_turn(store, project.id, "请求", "已回答")
    snap = store.record_snapshot(project.id, "snapshot")
    other = store.create_conversation()
    with sqlite3.connect(store.database) as db:
        db.execute("PRAGMA foreign_keys = ON")
        for statement, args in [
            ("UPDATE nodes SET user_text = ? WHERE id = ?", ("覆盖", first.node.id)),
            ("DELETE FROM nodes WHERE id = ?", (first.node.id,)),
            ("UPDATE runs SET answer = ? WHERE id = ?", ("覆盖", first.run.id)),
            ("DELETE FROM runs WHERE id = ?", (first.run.id,)),
            ("UPDATE snapshots SET storage_key = ? WHERE id = ?", ("覆盖", snap.id)),
            ("DELETE FROM snapshots WHERE id = ?", (snap.id,)),
            ("UPDATE conversations SET view_node_id = ? WHERE id = ?", (first.node.id, other.id)),
            (
                "UPDATE branches SET head_node_id = ? WHERE id = ?",
                (first.node.id, other.active_branch_id),
            ),
        ]:
            with pytest.raises(sqlite3.IntegrityError):
                db.execute(statement, args)
            db.rollback()
        assert db.execute("PRAGMA foreign_key_check").fetchall() == []
        assert db.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert db.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION


def test_exception_after_insert_rolls_back_entire_edit(store, chat, monkeypatch):
    original = completed_turn(store, chat.id, "原轮次")
    before = store.get_tree(chat.id)
    select = store._select

    def fail_after_cursor_update(*args):
        select(*args)
        raise RuntimeError("simulated interruption before transaction commit")

    monkeypatch.setattr(store, "_select", fail_after_cursor_update)
    with pytest.raises(RuntimeError, match="simulated interruption"):
        store.edit_turn(chat.id, original.node.id, "新请求")
    assert WorkbenchStore(store.database).get_tree(chat.id) == before


def test_concurrent_initialization_creates_one_valid_schema(tmp_path):
    path = tmp_path / "workbench.sqlite3"
    barrier = Barrier(2)

    def initialize(_):
        barrier.wait(timeout=10)
        return WorkbenchStore(path).create_conversation("新会话")

    with ThreadPoolExecutor(max_workers=2) as pool:
        created = list(pool.map(initialize, range(2)))
    reopened = WorkbenchStore(path)
    assert {c.id for c in reopened.list_conversations()} == {c.id for c in created}
    with sqlite3.connect(path) as db:
        assert db.execute("PRAGMA foreign_key_check").fetchall() == []
        assert db.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
