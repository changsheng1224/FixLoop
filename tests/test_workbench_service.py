"""S2 acceptance: ordinary chat, real ancestry, cancellation and durable recovery."""

import json
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict

import pytest

from agent_runtime.model_turn import FinishKind, ModelTurnResult, ProviderFinish
from src.workbench import ConflictError, NotFoundError, WorkbenchStore
from src.workbench.chat import ChatExecutor
from src.workbench.events import EventJournal
from src.workbench.service import UnsupportedModeError, WorkbenchService


def result(text):
    return ModelTurnResult(
        text=text,
        finish=ProviderFinish(FinishKind.TEXT_COMPLETE, "stop", "test"),
        usage={"input_tokens": 10, "output_tokens": 5},
    )


class Echo:
    def __init__(self):
        self.messages = []

    def __call__(self, messages, token, chunk):
        self.messages.append(messages)
        answer = "回答：" + messages[-1]["content"]
        chunk(answer[:3])
        chunk(answer[3:])
        return result(answer)


@pytest.fixture
def service(tmp_path):
    with WorkbenchService(tmp_path / "state", executor=Echo()) as service:
        yield service


def completed(service, cid, text):
    turn = service.send(cid, text)
    assert service.wait(cid, turn.run.id).status == "succeeded"
    return turn


def test_three_turn_edit_and_regenerate_preserve_original_future(service):
    chat = service.create_conversation("写作")
    first = completed(service, chat.id, "一")
    second = completed(service, chat.id, "二")
    third = completed(service, chat.id, "三")
    original = service.store.get_tree(chat.id)
    edited = service.edit(chat.id, second.node.id, "二改")
    assert service.wait(chat.id, edited.run.id).answer == "回答：二改"
    assert service.executor.messages[-1] == (
        {"role": "user", "content": "一"},
        {"role": "assistant", "content": "回答：一"},
        {"role": "user", "content": "二改"},
    )
    reexecuted = service.regenerate(chat.id, first.node.id)
    assert service.wait(chat.id, reexecuted.run.id).status == "succeeded"
    assert service.executor.messages[-1] == ({"role": "user", "content": "一"},)
    tree = service.store.get_tree(chat.id)
    assert len(tree.branches) == 3
    assert all(run in tree.runs for run in original.runs)
    assert service.store.get_run(chat.id, third.run.id) == original.runs[2]
    service.switch_branch(chat.id, chat.active_branch_id)
    continued = completed(service, chat.id, "四")
    assert continued.node.parent_id == third.node.id
    assert service.executor.messages[-1][-3:] == (
        {"role": "user", "content": "三"},
        {"role": "assistant", "content": "回答：三"},
        {"role": "user", "content": "四"},
    )


def test_browsing_does_not_change_execution_and_fork_does(service):
    chat = service.create_conversation()
    first = completed(service, chat.id, "A")
    second = completed(service, chat.id, "B")
    service.view(chat.id, first.node.id)
    assert completed(service, chat.id, "C").node.parent_id == second.node.id
    service.fork(chat.id, first.node.id)
    assert completed(service, chat.id, "D").node.parent_id == first.node.id
    service.fork(chat.id, None)
    assert completed(service, chat.id, "E").node.parent_id is None
    assert service.executor.messages[-1] == ({"role": "user", "content": "E"},)


def test_stream_journal_reopens_and_replay_cursor_is_scoped(service):
    chat = service.create_conversation()
    turn = completed(service, chat.id, "你好\n世界")
    events = service.events(chat.id, turn.run.id)
    assert [event["kind"] for event in events] == [
        "run.queued",
        "run.started",
        "answer.delta",
        "answer.delta",
        "run.finished",
    ]
    assert (
        "".join(e["data"]["text"] for e in events if e["kind"] == "answer.delta")
        == "回答：你好\n世界"
    )
    assert EventJournal(service.journal.path).read(turn.run.id, 2) == events[2:]
    assert service.events(chat.id, turn.run.id, after=5) == ()
    with pytest.raises(ValueError):
        service.events(chat.id, turn.run.id, after=99)
    other = service.create_conversation()
    with pytest.raises(NotFoundError):
        service.events(other.id, turn.run.id)


def test_stale_revision_and_project_execution_rejected_before_mutation(service, tmp_path):
    chat = service.create_conversation()
    service.rename_conversation(chat.id, "new")
    with pytest.raises(ConflictError):
        service.send(chat.id, "ignored", expected_revision=chat.revision)
    with pytest.raises(UnsupportedModeError):
        service.send(chat.id, "ignored", mode="repair")
    with pytest.raises(FileNotFoundError):
        service.create_conversation(project_path=str(tmp_path / "missing-repo"))
    assert service.store.get_tree(chat.id).nodes == ()
    project = service.store.create_conversation(project_path=str(tmp_path / "repo"))
    before = service.store.get_tree(project.id)
    for operation in [
        lambda: service.send(project.id, "question"),
        lambda: service.fork(project.id, None),
        lambda: service.switch_branch(project.id, project.active_branch_id),
    ]:
        with pytest.raises(UnsupportedModeError):
            operation()
    assert service.store.get_tree(project.id) == before


def test_cancel_preserves_partial_and_unblocks_history(tmp_path):
    started = threading.Event()

    def executor(messages, token, chunk):
        chunk("部分回答")
        started.set()
        while not token.is_cancelled:
            time.sleep(0.005)
        token.check()

    with WorkbenchService(tmp_path, executor=executor) as service:
        chat = service.create_conversation()
        turn = service.send(chat.id, "write")
        assert started.wait(2)
        with pytest.raises(ConflictError):
            service.send(chat.id, "other")
        with pytest.raises(ConflictError):
            service.fork(chat.id, None)
        service.view(chat.id, None)
        service.stop(chat.id, turn.run.id)
        run = service.wait(chat.id, turn.run.id)
        assert run.status == "cancelled" and run.answer == "部分回答"
        assert service.stop(chat.id, run.id) == run
        service.executor = Echo()
        assert completed(service, chat.id, "next").run.id != turn.run.id


@pytest.mark.parametrize("behavior", ["raise", "empty", "truncated", "mismatch", "too_large"])
def test_failure_is_durable_preserves_partial_and_never_claims_success(tmp_path, behavior):
    def executor(messages, token, chunk):
        chunk("partial")
        if behavior == "raise":
            raise RuntimeError("Bearer SECRET-shall-never-be-stored")
        if behavior == "too_large":
            chunk("x" * 100)
        if behavior == "truncated":
            return ModelTurnResult(
                text="partial", finish=ProviderFinish(FinishKind.MAX_OUTPUT_TOKENS)
            )
        return result("" if behavior == "empty" else "different")

    with WorkbenchService(tmp_path, executor=executor, max_output_bytes=32) as service:
        chat = service.create_conversation()
        turn = service.send(chat.id, "test")
        run = service.wait(chat.id, turn.run.id)
        assert run.status == "failed" and run.answer == "partial"
        assert "SECRET" not in json.dumps(asdict(run))
        assert WorkbenchStore(service.store.database).get_run(chat.id, run.id) == run


def test_service_ownership_does_not_interrupt_live_worker(tmp_path):
    started = threading.Event()

    def executor(messages, token, chunk):
        started.set()
        while not token.is_cancelled:
            time.sleep(0.005)
        token.check()

    with WorkbenchService(tmp_path, executor=executor) as service:
        chat = service.create_conversation()
        turn = service.send(chat.id, "test")
        assert started.wait(2)
        with pytest.raises(ConflictError):
            WorkbenchService(tmp_path, executor=Echo())
        probe = subprocess.run(
            [
                sys.executable,
                "-c",
                (
                    "from src.workbench.service import WorkbenchService; "
                    "from src.workbench import ConflictError; "
                    "import sys; "
                    "\ntry: WorkbenchService(sys.argv[1])\nexcept ConflictError: sys.exit(23)"
                ),
                str(tmp_path),
            ],
            capture_output=True,
            text=True,
            timeout=10,
        )
        assert probe.returncode == 23, probe.stderr
        assert service.store.get_run(chat.id, turn.run.id).status == "running"
    with WorkbenchService(tmp_path, executor=Echo()) as reopened:
        assert reopened.store.get_run(chat.id, turn.run.id).status == "interrupted"


def test_restart_recovers_queued_and_running_with_partial_without_replay(tmp_path):
    store = WorkbenchStore(tmp_path / "workbench.sqlite3")
    queued_chat = store.create_conversation("queued")
    queued = store.append_turn(queued_chat.id, "queued")
    running_chat = store.create_conversation("running")
    running = store.append_turn(running_chat.id, "running")
    store.start_run(running_chat.id, running.run.id)
    EventJournal(tmp_path / "events.sqlite3").publish(
        running.run.id, "answer.delta", {"text": "partial"}
    )
    executor = Echo()
    with WorkbenchService(tmp_path, executor=executor) as service:
        assert executor.messages == []
        assert service.store.get_run(queued_chat.id, queued.run.id).status == "interrupted"
        recovered = service.store.get_run(running_chat.id, running.run.id)
        assert recovered.status == "interrupted" and recovered.answer == "partial"
        assert service.events(running_chat.id, running.run.id)[-1]["kind"] == "run.finished"
        assert completed(service, running_chat.id, "continue").node.parent_id == running.node.id


def test_actual_process_crash_recovery(tmp_path):
    code = """
import os, sys, time
from src.workbench.service import WorkbenchService
def executor(messages, token, chunk):
    chunk('durable partial')
    os._exit(17)
service = WorkbenchService(sys.argv[1], executor=executor)
chat = service.create_conversation()
service.send(chat.id, 'question')
time.sleep(5)
"""
    crashed = subprocess.run([sys.executable, "-c", code, str(tmp_path)], timeout=10)
    assert crashed.returncode == 17
    with WorkbenchService(tmp_path, executor=Echo()) as service:
        chat = service.store.list_conversations()[0]
        run = service.store.get_tree(chat.id).runs[0]
        assert run.status == "interrupted" and run.answer == "durable partial"
        edited = service.regenerate(chat.id, run.node_id)
        assert service.wait(chat.id, edited.run.id).status == "succeeded"


def test_admission_capacity_and_conversation_independence(tmp_path):
    started = threading.Event()

    def blocking(messages, token, chunk):
        started.set()
        while not token.is_cancelled:
            time.sleep(0.005)
        token.check()

    with WorkbenchService(tmp_path, executor=blocking, max_active_runs=1) as service:
        first, second = service.create_conversation(), service.create_conversation()
        turn = service.send(first.id, "A")
        assert started.wait(2)
        with pytest.raises(ConflictError):
            service.send(second.id, "B")
        assert service.store.get_tree(second.id).nodes == ()
        service.stop(first.id, turn.run.id)
        service.wait(first.id, turn.run.id)


def test_close_retains_ownership_until_worker_exits(tmp_path):
    release = threading.Event()
    started = threading.Event()

    def executor(messages, token, chunk):
        started.set()
        release.wait(3)
        return result("late")

    service = WorkbenchService(tmp_path, executor=executor)
    chat = service.create_conversation()
    turn = service.send(chat.id, "test")
    assert started.wait(2)
    try:
        with pytest.raises(TimeoutError):
            service.close(timeout=0)
        with pytest.raises(ConflictError):
            WorkbenchService(tmp_path)
        with pytest.raises(ConflictError):
            service.send(chat.id, "late")
    finally:
        release.set()
        service.close()
    assert service.store.get_run(chat.id, turn.run.id).status == "interrupted"


def test_cancelled_blocking_model_io_is_fenced_and_capacity_bounded(tmp_path, monkeypatch):
    import src.workbench.chat as chat_module

    started = threading.Event()
    release = threading.Event()

    def transport(client, request, *, cancel_token, on_chunk):
        on_chunk("early")
        started.set()
        release.wait(3)
        on_chunk("late")
        return result("earlylate")

    monkeypatch.setattr(chat_module, "stream_chat_turn", transport)
    executor = ChatExecutor(lambda: object(), timeout_s=0.3, max_transports=1)
    with WorkbenchService(tmp_path, executor=executor) as service:
        chat = service.create_conversation()
        first = service.send(chat.id, "first")
        assert started.wait(2)
        service.stop(chat.id, first.run.id)
        assert service.wait(chat.id, first.run.id).answer == "early"
        second = service.send(chat.id, "second")
        assert service.wait(chat.id, second.run.id).status == "failed"
        assert service.store.get_run(chat.id, first.run.id).answer == "early"
        release.set()


def test_chat_context_limit_and_model_deadline(tmp_path, monkeypatch):
    import src.workbench.chat as chat_module

    executor = ChatExecutor(lambda: object(), max_context_bytes=1)
    with WorkbenchService(tmp_path / "context", executor=executor) as service:
        chat = service.create_conversation()
        turn = service.send(chat.id, "too long")
        assert service.wait(chat.id, turn.run.id).error == "ValueError"

    release = threading.Event()

    def transport(*args, **kwargs):
        release.wait(3)
        return result("late")

    monkeypatch.setattr(chat_module, "stream_chat_turn", transport)
    with WorkbenchService(
        tmp_path / "timeout", executor=ChatExecutor(lambda: object(), timeout_s=0.1)
    ) as service:
        chat = service.create_conversation()
        turn = service.send(chat.id, "test")
        assert service.wait(chat.id, turn.run.id).status == "failed"
        assert service.store.get_run(chat.id, turn.run.id).error == "TimeoutError"
    release.set()


def test_simultaneous_sends_admit_one_turn_without_duplicate_history(tmp_path):
    release = threading.Event()
    barrier = threading.Barrier(2)

    def executor(messages, token, chunk):
        assert release.wait(3)
        return result("answer")

    with WorkbenchService(tmp_path, executor=executor) as service:
        chat = service.create_conversation()

        def send(text):
            barrier.wait(2)
            try:
                return service.send(chat.id, text)
            except ConflictError:
                return None

        try:
            with ThreadPoolExecutor(max_workers=2) as pool:
                futures = [pool.submit(send, text) for text in ["A", "B"]]
                admitted = [turn for f in futures if (turn := f.result(timeout=2)) is not None]
            assert len(admitted) == 1
            tree = service.store.get_tree(chat.id)
            assert len(tree.nodes) == len(tree.runs) == 1
        finally:
            release.set()
        assert service.wait(chat.id, admitted[0].run.id).status == "succeeded"


def test_independent_chats_and_fresh_clients_never_share_context(tmp_path, monkeypatch):
    import src.workbench.chat as chat_module

    barrier = threading.Barrier(2)
    calls = []

    def transport(client, request, *, cancel_token, on_chunk):
        calls.append((client, request.messages))
        barrier.wait(2)
        text = "answer:" + request.messages[-1]["content"]
        on_chunk(text)
        return result(text)

    monkeypatch.setattr(chat_module, "stream_chat_turn", transport)
    with WorkbenchService(tmp_path, executor=ChatExecutor(object)) as service:
        first, second = service.create_conversation(), service.create_conversation()
        turns = [service.send(first.id, "private A"), service.send(second.id, "private B")]
        for turn in turns:
            assert service.wait(turn.run.conversation_id, turn.run.id).status == "succeeded"
        assert calls[0][0] is not calls[1][0]
        assert {tuple(m["content"] for m in messages) for _, messages in calls} == {
            ("private A",),
            ("private B",),
        }


def test_restart_repairs_missing_terminal_journal_without_reexecution(tmp_path):
    store = WorkbenchStore(tmp_path / "workbench.sqlite3")
    chat = store.create_conversation()
    turn = store.append_turn(chat.id, "question")
    store.start_run(chat.id, turn.run.id)
    finished = store.finish_run(chat.id, turn.run.id, status="succeeded", answer="answer")
    executor = Echo()
    with WorkbenchService(tmp_path, executor=executor) as service:
        assert service.store.get_run(chat.id, turn.run.id) == finished
        assert service.events(chat.id, turn.run.id)[-1]["data"]["answer"] == "answer"
        assert executor.messages == []
