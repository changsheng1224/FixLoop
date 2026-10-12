"""Incremental summaries must follow ancestry, budgets and project evidence versions."""

import re

import pytest

from agent_runtime.cancellation import CancellationToken, CancelledError
from src.workbench.context import ContextBuildError, ConversationContext
from src.workbench.store import WorkbenchStore


class Characters:
    def count(self, text):
        return len(text)

    def fit(self, text, limit):
        return text[:limit]


class Summarizer:
    def __init__(self):
        self.calls = []

    def __call__(self, data, token):
        token.check()
        self.calls.append(data)
        words = re.findall(r"KEY_[A-Z0-9]+", data["earlier_summary"] + data["conversation_data"])
        return " ".join(dict.fromkeys(words)) or "summary"


def completed(store, cid, text):
    turn = store.append_turn(cid, text)
    store.start_run(cid, turn.run.id)
    store.finish_run(cid, turn.run.id, answer="answer " * 10)
    return turn


def builder(store, summarizer):
    return ConversationContext(
        store,
        summarizer,
        counter=Characters(),
        budget_tokens=650,
        summary_tokens=100,
        recent_turns=1,
        summary_input_tokens=1800,
    )


def history(tmp_path):
    store = WorkbenchStore(tmp_path / "workbench.sqlite3")
    chat = store.create_conversation()
    turns = [completed(store, chat.id, f"KEY_{i} " + "detail " * 35) for i in range(8)]
    return store, chat, turns


def test_long_history_compacts_and_next_turn_uses_cached_prefix(tmp_path):
    store, chat, turns = history(tmp_path)
    summarize = Summarizer()
    context = builder(store, summarize)
    original = store.get_messages(chat.id, turns[-1].node.id)
    projected, metadata = context.build(chat.id, turns[-1].node.id, CancellationToken())
    assert metadata["sent_tokens"] <= 650 and metadata["summary_turns"] == 7
    assert projected[-2:][0]["content"] == turns[-1].node.user_text
    assert store.get_messages(chat.id, turns[-1].node.id) == original
    summarize.calls.clear()
    last = completed(store, chat.id, "new request")
    projected, metadata = context.build(chat.id, last.node.id, CancellationToken())
    assert metadata["cache_hits"] == 1 and len(summarize.calls) == 1
    assert projected[-2]["content"] == "new request"


def test_edit_reuses_only_common_ancestors_and_excludes_old_future(tmp_path):
    store, chat, turns = history(tmp_path)
    summarize = Summarizer()
    context = builder(store, summarize)
    context.build(chat.id, turns[-1].node.id, CancellationToken())
    edited = store.edit_turn(chat.id, turns[3].node.id, "KEY_EDIT " + "modified " * 30)
    store.start_run(chat.id, edited.run.id)
    store.finish_run(chat.id, edited.run.id, answer="edited answer")
    last = completed(store, chat.id, "KEY_NEW " + "new " * 50)
    summarize.calls.clear()
    projected, metadata = context.build(chat.id, last.node.id, CancellationToken())
    assert metadata["cache_hits"] == 1
    text = "\n".join(m["content"] for m in projected)
    assert "KEY_EDIT" in text and "KEY_6" not in text and "KEY_7" not in text
    assert all(
        "KEY_6" not in c["conversation_data"] and "KEY_7" not in c["conversation_data"]
        for c in summarize.calls
    )
    assert store.get_run(chat.id, turns[-1].run.id).answer


def test_snapshot_change_invalidates_project_summary_cache(tmp_path):
    store, chat, turns = history(tmp_path)
    summarize = Summarizer()
    context = builder(store, summarize)
    context.build(chat.id, turns[-1].node.id, CancellationToken(), snapshot_key="before")
    summarize.calls.clear()
    _, metadata = context.build(
        chat.id, turns[-1].node.id, CancellationToken(), snapshot_key="after"
    )
    assert metadata["cache_hits"] == 0 and len(summarize.calls) == 7


def test_failure_or_cancel_never_drops_history_or_caches_incomplete_summary(tmp_path):
    store, chat, turns = history(tmp_path)
    original = store.get_messages(chat.id, turns[-1].node.id)

    def failed(data, token):
        raise RuntimeError("provider failure")

    with pytest.raises(ContextBuildError, match="original history preserved"):
        builder(store, failed).build(chat.id, turns[-1].node.id, CancellationToken())
    token = CancellationToken()

    def cancelled(data, cancel):
        cancel.cancel("user")
        return "partial summary"

    with pytest.raises(CancelledError):
        builder(store, cancelled).build(chat.id, turns[-1].node.id, token)
    with store._transaction(write=False) as db:
        assert db.execute("SELECT COUNT(*) FROM context_summaries").fetchone()[0] == 0
    assert store.get_messages(chat.id, turns[-1].node.id) == original


def test_oversized_current_request_is_never_silently_trimmed(tmp_path):
    store = WorkbenchStore(tmp_path / "workbench.sqlite3")
    chat = store.create_conversation()
    turn = store.append_turn(chat.id, "important request " * 100)
    with pytest.raises(ContextBuildError, match="current request exceeds"):
        builder(store, Summarizer()).build(chat.id, turn.node.id, CancellationToken())
    assert store.get_path(chat.id, turn.node.id)[0].user_text == turn.node.user_text


def test_large_historical_turn_is_consumed_in_bounded_segments(tmp_path):
    store = WorkbenchStore(tmp_path / "workbench.sqlite3")
    chat = store.create_conversation()
    completed(store, chat.id, "KEY_START " + "detail " * 1000 + "KEY_END")
    last = completed(store, chat.id, "small current request")
    summarize = Summarizer()
    projected, metadata = builder(store, summarize).build(
        chat.id, last.node.id, CancellationToken()
    )
    assert len(summarize.calls) > 1
    assert "KEY_START" in projected[0]["content"] and "KEY_END" in projected[0]["content"]
    assert metadata["sent_tokens"] <= 650
