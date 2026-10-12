"""Production subprocess workers and hard stop against blocking loopback I/O."""

import os
import subprocess
import sys
import time

import pytest

from agent_runtime.cancellation import CancellationToken
from agent_runtime.plan_runtime.processes import confirmed_exited, process_identity
from src.workbench.chat import ChatExecutor
from src.workbench.models import ConflictError
from src.workbench.process_scope import WindowsJob
from src.workbench.project import ProjectExecutor
from src.workbench.service import UnsupportedModeError, WorkbenchService
from src.workbench.store import WorkbenchStore
from tests.workbench_e2e_support import ModelServer, WorkbenchProcess, eventually


@pytest.mark.skipif(os.name != "nt", reason="actual Windows process containment")
@pytest.mark.parametrize("stop", ["user", "timeout"])
def test_production_worker_hard_stops_blocked_transport(tmp_path, stop):
    with ModelServer(tmp_path / "provider") as model:
        model.script([{"text": "late answer", "delay_s": 30}])
        executor = ChatExecutor(
            provider="ollama",
            model="test",
            base_url=model.url,
            timeout_s=4 if stop == "timeout" else 60,
        )
        with WorkbenchService(
            tmp_path / "state", executor=executor, project_lock_dir=tmp_path / "locks"
        ) as service:
            chat = service.create_conversation()
            turn = service.send(chat.id, "blocked request")
            eventually(lambda: model.snapshot()["requests"], timeout=15)
            identity = service.store.resource(turn.run.id)["worker"]
            assert identity["pid"] != os.getpid()
            started = time.monotonic()
            if stop == "user":
                service.stop(chat.id, turn.run.id)
            run = service.wait(chat.id, turn.run.id, timeout=10)
            assert run.status == "cancelled" and run.error == stop
            assert time.monotonic() - started < 7
            assert confirmed_exited(identity)
            assert service.store.resource(run.id)["state"] == "stopped"
            assert not run.answer
            assert [e["kind"] for e in service.events(chat.id, run.id)].count("run.finished") == 1


def test_host_repair_rejected_before_history_mutation(tmp_path):
    project = tmp_path / "project"
    project.mkdir()
    with WorkbenchService(
        tmp_path / "state",
        project_executor=ProjectExecutor(execution_tier="host"),
        project_lock_dir=tmp_path / "locks",
    ) as service:
        chat = service.create_conversation(project_path=str(project))
        with pytest.raises(UnsupportedModeError, match="filesystem sandbox"):
            service.send(chat.id, "fix", mode="repair")
        assert not service.store.get_tree(chat.id).nodes


@pytest.mark.skipif(os.name != "nt", reason="actual named worker recovery")
def test_restart_reaps_live_writer_before_capturing_workspace(tmp_path):
    project = tmp_path / "project"
    project.mkdir()
    (project / "value.txt").write_text("user")
    scope, process = WindowsJob(), None
    try:
        with WorkbenchService(tmp_path / "state", project_lock_dir=tmp_path / "locks") as service:
            chat = service.create_conversation(project_path=str(project))
            turn = service._append(chat.id, "old", "repair", None)
            tree = service.store.get_tree(chat.id)
            snapshot = next(s for s in tree.snapshots if s.id == turn.node.input_snapshot_id)
            work = service.workspaces.create(turn.run.id, snapshot.manifest, CancellationToken())
            script = "import pathlib,sys,time;p=pathlib.Path(sys.argv[1]);p.write_text('partial');time.sleep(30);p.write_text('late')"
            process = subprocess.Popen(
                [sys.executable, "-c", script, str(work / "value.txt")],
                creationflags=subprocess.CREATE_NO_WINDOW,
            )
            scope.attach(process.pid)
            identity = process_identity(process.pid)
            service.store.start_run(chat.id, turn.run.id)
            service.store.save_resource(
                turn.run.id,
                {
                    **scope.descriptor(),
                    "worker": identity,
                    "state": "running",
                    "run_id": turn.run.id,
                    "state_root": str(service.state_dir / "runtime" / turn.run.id),
                    "workspace": str(work),
                    "project_path": str(project),
                    "root_identity": snapshot.manifest["root_identity"],
                },
            )
            eventually(lambda: (work / "value.txt").read_text() == "partial")
        assert process.poll() is None  # controller ownership is free, writer remains live
        with WorkbenchService(tmp_path / "state", project_lock_dir=tmp_path / "locks") as service:
            run = service.store.get_run(chat.id, turn.run.id)
            assert run.status == "interrupted" and run.output_snapshot_id
            assert service.store.resource(run.id)["state"] == "stopped"
            assert confirmed_exited(identity)
            assert (project / "value.txt").read_text() == "user"
            assert (work / "value.txt").read_text() == "partial"
    finally:
        scope.close()
        if process:
            process.wait(timeout=5)


def test_unknown_resource_blocks_all_conversations_on_project(tmp_path, monkeypatch):
    project = tmp_path / "project"
    project.mkdir()
    with WorkbenchService(tmp_path / "state", project_lock_dir=tmp_path / "locks") as service:
        first = service.create_conversation(project_path=str(project))
        second = service.create_conversation(project_path=str(project))
        turn = service.store.append_turn(first.id, "legacy", mode="repair")
        service.store.start_run(first.id, turn.run.id)
        service.store.save_resource(
            turn.run.id, {"state": "running", "backend": "unknown", "run_id": turn.run.id}
        )
    with WorkbenchService(tmp_path / "state", project_lock_dir=tmp_path / "locks") as service:
        run = service.store.get_run(first.id, turn.run.id)
        assert run.error == "execution_cleanup_unconfirmed" and not run.output_snapshot_id
        assert (
            service.project_status(second.id)["execution_blockers"][0]["conversation_id"]
            == first.id
        )
        for cid in (first.id, second.id):
            with pytest.raises(ConflictError, match="cleanup"):
                service.send(cid, "question", mode="project_chat")
        with pytest.raises(ConflictError, match="remains unconfirmed"):
            service.reconcile_resources(first.id, turn.run.id)
        assert service.project_status(second.id)["execution_blockers"]
        monkeypatch.setattr("src.workbench.service.reconcile_execution", lambda resource: True)
        service.reconcile_resources(first.id, turn.run.id)
        assert not service.project_status(second.id)["execution_blockers"]


@pytest.mark.skipif(os.name != "nt", reason="actual Windows supervised context")
def test_production_context_summaries_are_incremental_and_bounded(tmp_path):
    with ModelServer(tmp_path / "provider") as model:
        executor = ChatExecutor(provider="ollama", model="test", base_url=model.url, timeout_s=30)
        with WorkbenchService(
            tmp_path / "state", executor=executor, project_lock_dir=tmp_path / "locks"
        ) as service:
            chat = service.create_conversation()
            for index in range(5):
                turn = service.store.append_turn(chat.id, f"KEEP_{index} " + "alpha " * 2500)
                service.store.start_run(chat.id, turn.run.id)
                service.store.finish_run(chat.id, turn.run.id, answer="prior answer")
            model.script(
                [{"text": f"summary KEEP_{i}"} for i in range(3)] + [{"text": "final answer"}]
            )
            turn = service.send(chat.id, "current request")
            run = service.wait(chat.id, turn.run.id, timeout=25)
            assert run.status == "succeeded", run
            assert run.artifacts["context"]["summary_turns"] == 3
            assert run.artifacts["context"]["sent_tokens"] <= 8000
            assert len(model.snapshot()["requests"]) == 4
            model.script([{"text": "incremental KEEP_3"}, {"text": "next answer"}])
            turn = service.send(chat.id, "next request")
            run = service.wait(chat.id, turn.run.id, timeout=25)
            assert run.status == "succeeded", run
            assert run.artifacts["context"]["cache_hits"] == 1
            assert len(model.snapshot()["requests"]) == 6
            assert not model.snapshot()["remaining"]
            assert len(service.store.get_tree(chat.id).nodes) == 7


@pytest.mark.skipif(os.name != "nt", reason="actual Windows service death and Job lifetime")
def test_service_death_stops_blocking_worker_before_restart(tmp_path):
    with ModelServer(tmp_path / "provider") as model:
        model.script([{"text": "late answer", "delay_s": 30}])
        process = WorkbenchProcess(tmp_path / "controller", model.url, timeout=60)
        with process:
            chat = process.create()
            turn = process.turn(chat["id"], "block transport")
            eventually(lambda: model.snapshot()["requests"], timeout=15)
            store = WorkbenchStore(process.root / "state/workbench.sqlite3")
            identity = store.resource(turn["run"]["id"])["worker"]
            process.kill()
            eventually(lambda: confirmed_exited(identity), timeout=6)
        with process:
            run = process.api(f"/conversations/{chat['id']}/runs/{turn['run']['id']}")["run"]
            assert run["status"] == "interrupted" and not run["answer"]
            assert store.resource(run["id"])["state"] == "stopped"
            assert not process.api(f"/conversations/{chat['id']}/workspace")["execution_blockers"]


@pytest.mark.skipif(os.name != "nt", reason="actual Windows supervised parallel project workers")
def test_production_project_conversations_run_simultaneously(tmp_path):
    project = tmp_path / "project"
    project.mkdir()
    (project / "value.txt").write_bytes(b"original")
    with ModelServer(tmp_path / "provider") as model:
        model.script(
            [
                {"text": "<final>first answer</final>", "delay_s": 3},
                {"text": "<final>second answer</final>", "delay_s": 3},
            ]
        )
        executor = ProjectExecutor(
            provider="ollama", model="test", base_url=model.url, timeout_s=20
        )
        with WorkbenchService(
            tmp_path / "state", project_executor=executor, project_lock_dir=tmp_path / "locks"
        ) as service:
            chats = [service.create_conversation(project_path=str(project)) for _ in range(2)]
            turns = [
                service.send(chat.id, "independent question", mode="project_chat") for chat in chats
            ]
            eventually(lambda: len(model.snapshot()["requests"]) == 2, timeout=12)
            resources = [service.store.resource(turn.run.id) for turn in turns]
            assert len({r["workspace"] for r in resources}) == 2
            assert len({r["worker"]["pid"] for r in resources}) == 2
            assert all(
                service.store.get_run(t.run.conversation_id, t.run.id).status == "running"
                for t in turns
            )
            assert (project / "value.txt").read_bytes() == b"original"
            for turn in turns:
                run = service.wait(turn.run.conversation_id, turn.run.id, timeout=15)
                assert run.status == "succeeded", run
                assert not service.project_status(turn.run.conversation_id)["candidates"]
