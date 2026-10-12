"""Repairs produce isolated candidates and write only after reviewed application."""

import threading

import pytest

from src.workbench.models import ConflictError
from src.workbench.project import ProjectResult
from tests.test_workbench_service import Echo
from tests.workbench_unit_service import WorkbenchService


def repair(**kw):
    (kw["files"].root / "value.txt").write_text(kw["messages"][-1]["content"])
    return ProjectResult("done")


def setup(tmp_path, executor=repair):
    project = tmp_path / "project"
    project.mkdir()
    (project / "value.txt").write_text("manual input")
    service = WorkbenchService(
        tmp_path / "state",
        executor=Echo(),
        project_executor=executor,
        project_lock_dir=tmp_path / "locks",
    )
    return project, service


def test_project_untouched_until_reviewed_apply(tmp_path):
    project, service = setup(tmp_path)
    with service:
        chat = service.create_conversation(project_path=str(project))
        turn = service.send(chat.id, "fixed", mode="repair")
        run = service.wait(chat.id, turn.run.id)
        assert run.status == "succeeded"
        assert (project / "value.txt").read_text() == "manual input"
        candidate = service.project_status(chat.id)["candidates"][0]
        assert candidate["run_id"] == run.id and candidate["status"] == "ready"
        plan = service.preview_candidate(chat.id, run.id)
        assert "-manual input" in plan["diff"] and "+fixed" in plan["diff"]
        assert (project / "value.txt").read_text() == "manual input"
        service.apply_restore(chat.id, plan["id"])
        assert (project / "value.txt").read_text() == "fixed"
        assert service.project_status(chat.id)["candidates"][0]["status"] == "applied"


def test_parallel_conversations_have_independent_candidates(tmp_path):
    barrier = threading.Barrier(3)
    roots = []

    def simultaneous(**kw):
        roots.append(kw["files"].root)
        (kw["files"].root / "value.txt").write_text(kw["messages"][-1]["content"])
        barrier.wait(timeout=5)
        barrier.wait(timeout=5)
        return ProjectResult("done")

    project, service = setup(tmp_path, simultaneous)
    with service:
        first = service.create_conversation("first", project_path=str(project))
        second = service.create_conversation("second", project_path=str(project))
        turns = [
            service.send(chat.id, text, mode="repair")
            for chat, text in ((first, "first fix"), (second, "second fix"))
        ]
        barrier.wait(timeout=5)
        assert len(set(roots)) == 2 and project not in roots
        assert (project / "value.txt").read_text() == "manual input"
        assert all(
            service.store.get_run(t.run.conversation_id, t.run.id).status == "running"
            for t in turns
        )
        barrier.wait(timeout=5)
        for turn in turns:
            assert service.wait(turn.run.conversation_id, turn.run.id).status == "succeeded"
        plan = service.preview_candidate(first.id, turns[0].run.id)
        service.apply_restore(first.id, plan["id"])
        assert (project / "value.txt").read_text() == "first fix"
        with pytest.raises(ConflictError):
            service.preview_candidate(second.id, turns[1].run.id)
        assert service.project_status(second.id)["candidates"][0]["status"] == "ready"


def test_external_edit_preserved_with_candidate(tmp_path):
    project, service = setup(tmp_path)
    with service:
        chat = service.create_conversation(project_path=str(project))
        turn = service.send(chat.id, "fixed", mode="repair")
        service.wait(chat.id, turn.run.id)
        plan = service.preview_candidate(chat.id, turn.run.id)
        (project / "value.txt").write_text("editor")
        with pytest.raises(ConflictError):
            service.apply_restore(chat.id, plan["id"])
        assert (project / "value.txt").read_text() == "editor"
        assert service.project_status(chat.id)["candidates"][0]["status"] == "ready"


def test_cancellation_keeps_partial_only_in_workspace(tmp_path):
    started, release = threading.Event(), threading.Event()

    def blocking(**kw):
        (kw["files"].root / "value.txt").write_text("partial")
        started.set()
        assert release.wait(5)
        kw["token"].check()

    project, service = setup(tmp_path, blocking)
    with service:
        chat = service.create_conversation(project_path=str(project))
        turn = service.send(chat.id, "request", mode="repair")
        assert started.wait(5)
        service.stop(chat.id, turn.run.id)
        assert (project / "value.txt").read_text() == "manual input"
        release.set()
        run = service.wait(chat.id, turn.run.id)
        assert run.status == "cancelled" and run.artifacts["changed_files"] == ["value.txt"]
        assert not service.project_status(chat.id)["candidates"]
