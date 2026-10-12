"""Multi-file candidate application remains gated and recoverable at every boundary."""

import pytest

from src.workbench.models import ConflictError
from src.workbench.project import ProjectResult
from tests.workbench_unit_service import WorkbenchService


def executor(**kw):
    for name in ("a.txt", "b.txt"):
        (kw["files"].root / name).write_text("new")
    return ProjectResult("candidate")


@pytest.mark.parametrize("point", ["before_step", "after_step", "before_commit", "after_commit"])
@pytest.mark.parametrize("decision", ["complete", "rollback"])
def test_candidate_application_crash_boundaries(tmp_path, point, decision):
    root = tmp_path / "project"
    root.mkdir()
    for name in ("a.txt", "b.txt"):
        (root / name).write_text("old")

    def fault(where, index):
        if where == point and index in (0, -1):
            raise RuntimeError("injected crash")

    kwargs = {"project_executor": executor, "project_lock_dir": tmp_path / "locks"}
    with WorkbenchService(tmp_path / "state", restore_fault=fault, **kwargs) as service:
        chat = service.create_conversation(project_path=str(root))
        observer = service.create_conversation(project_path=str(root))
        turn = service.send(chat.id, "change both files", mode="repair")
        run = service.wait(chat.id, turn.run.id)
        plan = service.preview_candidate(chat.id, run.id)
        with pytest.raises(RuntimeError, match="injected"):
            service.apply_restore(chat.id, plan["id"])
        assert service.project_status(observer.id)["pending"]
        with pytest.raises(ConflictError):
            service.send(observer.id, "anything", mode="project_chat")
        assert service.store.get_run(chat.id, run.id).status == "succeeded"
    with WorkbenchService(tmp_path / "state", **kwargs) as service:
        if point in {"before_commit", "after_commit"}:
            assert not service.project_status(chat.id)["pending"]
            expected, candidate_status = "new", "applied"
        else:
            assert service.project_status(observer.id)["pending"]
            service.recover_restore(observer.id, plan["id"], decision=decision)
            expected = "new" if decision == "complete" else "old"
            candidate_status = "applied" if decision == "complete" else "ready"
        assert [(root / name).read_text() for name in ("a.txt", "b.txt")] == [expected, expected]
        assert not service.project_status(observer.id)["pending"]
        assert service.store.candidate(chat.id, run.id)["status"] == candidate_status


def test_external_edit_during_recovery_keeps_every_project_conversation_blocked(tmp_path):
    root = tmp_path / "project"
    root.mkdir()
    for name in ("a.txt", "b.txt"):
        (root / name).write_text("old")

    def fault(where, index):
        if where == "after_step" and index == 0:
            raise RuntimeError("crash")

    kwargs = {"project_executor": executor, "project_lock_dir": tmp_path / "locks"}
    with WorkbenchService(tmp_path / "state", restore_fault=fault, **kwargs) as service:
        chat = service.create_conversation(project_path=str(root))
        observer = service.create_conversation(project_path=str(root))
        turn = service.send(chat.id, "fix", mode="repair")
        service.wait(chat.id, turn.run.id)
        plan = service.preview_candidate(chat.id, turn.run.id)
        with pytest.raises(RuntimeError):
            service.apply_restore(chat.id, plan["id"])
    (root / "b.txt").write_text("external editor")
    with WorkbenchService(tmp_path / "state", **kwargs) as service:
        for decision in ("complete", "rollback"):
            with pytest.raises(ConflictError, match="preserved"):
                service.recover_restore(observer.id, plan["id"], decision=decision)
        assert (root / "b.txt").read_text() == "external editor"
        assert service.project_status(chat.id)["pending"]
        assert service.project_status(observer.id)["pending"]


def test_checkpoint_invalidates_other_conversations_prepared_write(tmp_path):
    root = tmp_path / "project"
    root.mkdir()
    (root / "a.txt").write_text("old")
    with WorkbenchService(
        tmp_path / "state", project_executor=executor, project_lock_dir=tmp_path / "locks"
    ) as service:
        first = service.create_conversation(project_path=str(root))
        second = service.create_conversation(project_path=str(root))
        plan = service.preview_restore(first.id, action="fork")
        service.save_checkpoint(second.id)
        with pytest.raises(ConflictError, match="project changed"):
            service.apply_restore(first.id, plan["id"])


def test_restart_preserves_durable_rollback_decision(tmp_path):
    root = tmp_path / "project"
    root.mkdir()
    for name in ("a.txt", "b.txt"):
        (root / name).write_text("old")

    def fault(where, index):
        if where == "before_commit" or (where == "recovery_before_step" and index == 0):
            raise RuntimeError("crash")

    kwargs = {"project_executor": executor, "project_lock_dir": tmp_path / "locks"}
    with WorkbenchService(tmp_path / "state", restore_fault=fault, **kwargs) as service:
        chat = service.create_conversation(project_path=str(root))
        turn = service.send(chat.id, "fix", mode="repair")
        service.wait(chat.id, turn.run.id)
        plan = service.preview_candidate(chat.id, turn.run.id)
        with pytest.raises(RuntimeError):
            service.apply_restore(chat.id, plan["id"])
        with pytest.raises(RuntimeError):
            service.recover_restore(chat.id, plan["id"], decision="rollback")
        assert [(root / name).read_text() for name in ("a.txt", "b.txt")] == ["new", "new"]
    with WorkbenchService(tmp_path / "state", **kwargs) as service:
        assert service.project_status(chat.id)["pending"][0]["recovery_decision"] == "rollback"
        assert service.store.candidate(chat.id, turn.run.id)["status"] == "ready"
        with pytest.raises(ConflictError, match="already chosen"):
            service.recover_restore(chat.id, plan["id"], decision="complete")
        service.recover_restore(chat.id, plan["id"], decision="rollback")
        assert [(root / name).read_text() for name in ("a.txt", "b.txt")] == ["old", "old"]
        assert not service.project_status(chat.id)["pending"]
