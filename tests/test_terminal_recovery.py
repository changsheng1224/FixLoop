"""Terminal persistence can fail without stranding a stopped conversation."""

import time

import pytest

from src.workbench.models import ConflictError
from tests.test_workbench_service import Echo
from tests.workbench_unit_service import WorkbenchService


def stopped(service, run_id):
    deadline = time.monotonic() + 5
    while run_id in service._active:
        assert time.monotonic() < deadline
        time.sleep(0.01)


@pytest.mark.parametrize("retry", ["stop", "send", "restart"])
def test_terminal_failure_recovered(tmp_path, monkeypatch, retry):
    directory = tmp_path / "state"
    service = WorkbenchService(directory, executor=Echo())
    cid = service.create_conversation().id
    finish = service.store.finish_run
    monkeypatch.setattr(
        service.store, "finish_run", lambda *a, **k: (_ for _ in ()).throw(OSError("disk"))
    )
    turn = service.send(cid, "first")
    stopped(service, turn.run.id)
    assert service.store.get_run(cid, turn.run.id).status == "running"
    assert list((directory / "pending-terminals").glob("*.json"))
    with pytest.raises(ConflictError, match="persistence pending"):
        service.stop(cid, turn.run.id)
    if retry == "restart":
        service.close()
        service = WorkbenchService(directory, executor=Echo())
    else:
        monkeypatch.setattr(service.store, "finish_run", finish)
        if retry == "send":
            second = service.send(cid, "second")
            assert service.wait(cid, second.run.id).status == "succeeded"
        else:
            assert service.stop(cid, turn.run.id).status == "succeeded"
    try:
        assert service.store.get_run(cid, turn.run.id).answer == "回答：first"
        assert not list((directory / "pending-terminals").glob("*.json"))
        assert sum(e["kind"] == "run.finished" for e in service.events(cid, turn.run.id)) == 1
    finally:
        service.close()


def test_failure_after_commit_does_not_duplicate_terminal(tmp_path, monkeypatch):
    with WorkbenchService(tmp_path / "state", executor=Echo()) as service:
        cid = service.create_conversation().id
        finish = service.store.finish_run

        def committed_then_failed(*args, **kwargs):
            finish(*args, **kwargs)
            raise OSError("after commit")

        monkeypatch.setattr(service.store, "finish_run", committed_then_failed)
        turn = service.send(cid, "first")
        stopped(service, turn.run.id)
        assert service.stop(cid, turn.run.id).status == "succeeded"
        assert sum(e["kind"] == "run.finished" for e in service.events(cid, turn.run.id)) == 1
