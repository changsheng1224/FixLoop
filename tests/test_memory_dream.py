"""Memory Dream 后台任务单测（V1.4-Bonus9b）。"""

from __future__ import annotations

import time

import pytest

from agent_runtime.features.memory.dream import (
    MemoryDreamer,
    _DEFAULT_TTL_DAYS,
    dream_summary_to_trace,
    run_memory_dream,
)
from agent_runtime.features.memory.core import MAX_EPISODIC_NOTES, default_memory_state


def _make_note(text: str, index: int, created_at: float | None = None) -> dict:
    return {
        "text": text,
        "note_index": index,
        "created_at": created_at or time.time(),
    }


# ---------------------------------------------------------------------------
# 去重
# ---------------------------------------------------------------------------


class TestDeduplicate:
    def test_no_duplicates(self):
        state = default_memory_state()
        state["episodic_notes"] = [
            _make_note("note a", 1),
            _make_note("note b", 2),
        ]
        dreamer = MemoryDreamer(state)
        removed = dreamer._deduplicate()
        assert removed == 0
        assert len(state["episodic_notes"]) == 2

    def test_removes_duplicates(self):
        state = default_memory_state()
        state["episodic_notes"] = [
            _make_note("note a", 1),
            _make_note("note a", 2),  # duplicate
            _make_note("note b", 3),
            _make_note("note a", 4),  # duplicate, keeps last
        ]
        dreamer = MemoryDreamer(state)
        removed = dreamer._deduplicate()
        assert removed == 2
        remaining = {n["note_index"] for n in state["episodic_notes"]}
        assert remaining == {3, 4}  # 最新的 b 和最新的 a

    def test_empty_state(self):
        state = default_memory_state()
        dreamer = MemoryDreamer(state)
        assert dreamer._deduplicate() == 0

    def test_empty_text_notes_ignored(self):
        state = default_memory_state()
        state["episodic_notes"] = [
            _make_note("", 1),
            _make_note("", 2),
            _make_note("valid", 3),
        ]
        dreamer = MemoryDreamer(state)
        dreamer._deduplicate()
        assert len(state["episodic_notes"]) == 1
        assert state["episodic_notes"][0]["note_index"] == 3


# ---------------------------------------------------------------------------
# 过期
# ---------------------------------------------------------------------------


class TestExpire:
    def test_no_expiry_when_all_fresh(self):
        state = default_memory_state()
        state["episodic_notes"] = [
            _make_note("fresh", 1, created_at=time.time()),
        ]
        dreamer = MemoryDreamer(state)
        removed = dreamer._expire(ttl_days=30)
        assert removed == 0

    def test_expires_old_notes(self):
        state = default_memory_state()
        old_time = time.time() - 60 * 86400  # 60 days ago
        state["episodic_notes"] = [
            _make_note("old", 1, created_at=old_time),
            _make_note("fresh", 2, created_at=time.time()),
        ]
        dreamer = MemoryDreamer(state)
        removed = dreamer._expire(ttl_days=30)
        assert removed == 1
        remaining = {n["note_index"] for n in state["episodic_notes"]}
        assert remaining == {2}

    def test_zero_ttl_skips(self):
        state = default_memory_state()
        state["episodic_notes"] = [_make_note("x", 1)]
        dreamer = MemoryDreamer(state)
        assert dreamer._expire(ttl_days=0) == 0

    def test_no_created_at_kept(self):
        state = default_memory_state()
        state["episodic_notes"] = [
            _make_note("no timestamp", 1, created_at=None),
        ]
        # remove created_at key
        del state["episodic_notes"][0]["created_at"]
        dreamer = MemoryDreamer(state)
        removed = dreamer._expire(ttl_days=30)
        assert removed == 0  # 无 created_at → 不过期


# ---------------------------------------------------------------------------
# 裁剪
# ---------------------------------------------------------------------------


class TestTrim:
    def test_within_limit(self):
        state = default_memory_state()
        state["episodic_notes"] = [_make_note(f"n{i}", i) for i in range(5)]
        dreamer = MemoryDreamer(state)
        assert dreamer._trim() == 0

    def test_exceeds_limit(self):
        state = default_memory_state()
        n = MAX_EPISODIC_NOTES + 5
        state["episodic_notes"] = [_make_note(f"n{i}", i) for i in range(n)]
        dreamer = MemoryDreamer(state)
        removed = dreamer._trim()
        assert removed == 5
        assert len(state["episodic_notes"]) == MAX_EPISODIC_NOTES
        # 保留最新的
        assert state["episodic_notes"][-1]["note_index"] == n - 1


# ---------------------------------------------------------------------------
# run / 便捷函数
# ---------------------------------------------------------------------------


class TestRun:
    def test_run_all_steps(self):
        state = default_memory_state()
        old = time.time() - 60 * 86400
        state["episodic_notes"] = [
            _make_note("dup", 1, created_at=time.time()),
            _make_note("dup", 2, created_at=time.time()),
            _make_note("old", 3, created_at=old),
            _make_note("unique", 4, created_at=time.time()),
        ]
        stats = run_memory_dream(state)
        assert stats["deduped"] == 1  # "dup" appeared twice
        assert stats["expired"] == 1  # "old" is 60 days old
        assert stats["total_before"] == 4
        assert stats["total_after"] == 2

    def test_run_without_expiry(self):
        state = default_memory_state()
        old = time.time() - 60 * 86400
        state["episodic_notes"] = [
            _make_note("old", 1, created_at=old),
        ]
        stats = run_memory_dream(state, ttl_days=-1)  # skip expire
        assert stats["expired"] == 0
        assert stats["total_after"] == 1


# ---------------------------------------------------------------------------
# trace
# ---------------------------------------------------------------------------


class TestDreamSummaryToTrace:
    def test_all_fields(self):
        trace = dream_summary_to_trace({
            "deduped": 2, "expired": 1, "trimmed": 0,
            "total_before": 10, "total_after": 7,
        })
        assert trace["deduped"] == 2
        assert trace["expired"] == 1
        assert trace["total_before"] == 10
        assert trace["total_after"] == 7


# ---------------------------------------------------------------------------
# AgentLoop 集成
# ---------------------------------------------------------------------------


class TestDreamInAgentLoop:
    def test_agent_ask_triggers_dream(self, temp_workspace):
        """agent.ask() 结束后触发 memory dream。"""
        from agent_runtime.config import AgentConfig
        from agent_runtime.providers.clients import FakeModelClient
        from agent_runtime.runtime import Agent
        from agent_runtime.workspace import WorkspaceContext

        ws = WorkspaceContext.build(str(temp_workspace))
        agent = Agent(
            config=AgentConfig(),
            model_client=FakeModelClient(outputs=["<final>ok</final>"]),
            workspace=ws,
            cwd=str(temp_workspace),
        )
        # 填充重复笔记
        agent.session["memory"]["episodic_notes"] = [
            _make_note("dup", 1, time.time()),
            _make_note("dup", 2, time.time()),
        ]
        answer = agent.ask("test")
        assert "ok" in answer
        notes = agent.session["memory"]["episodic_notes"]
        assert len(notes) == 1  # 去重后只剩 1 条
