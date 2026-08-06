"""Memory Dream 后台任务单测（V1.4-Bonus9b）。"""

from __future__ import annotations

import tempfile
import time
from pathlib import Path

from agent_runtime.features.memory.core import MAX_EPISODIC_NOTES, default_memory_state
from agent_runtime.features.memory.dream import (
    MAX_DURABLE_ENTRIES_PER_TOPIC,
    MemoryDreamer,
    dream_summary_to_trace,
    run_memory_dream,
)


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
        stats, dreamer = run_memory_dream(state)
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
        stats, dreamer = run_memory_dream(state, ttl_days=-1)  # skip expire
        assert stats["expired"] == 0
        assert stats["total_after"] == 1


# ---------------------------------------------------------------------------
# trace
# ---------------------------------------------------------------------------


class TestDreamSummaryToTrace:
    def test_all_fields(self):
        trace = dream_summary_to_trace(
            {
                "deduped": 2,
                "expired": 1,
                "trimmed": 0,
                "total_before": 10,
                "total_after": 7,
            }
        )
        assert trace["deduped"] == 2
        assert trace["expired"] == 1
        assert trace["total_before"] == 10
        assert trace["total_after"] == 7


# ---------------------------------------------------------------------------
# Durable GC
# ---------------------------------------------------------------------------


class TestDurableGC:
    def test_within_limit_unchanged(self):
        with tempfile.TemporaryDirectory() as tmp:
            _setup_topics_dir(tmp, 5)
            state = default_memory_state()
            dreamer = MemoryDreamer(state, durable_root=tmp)
            removed = dreamer._gc_durable(max_entries=10)
            assert removed == 0
            entries = _count_entries(Path(tmp))
            assert entries == 5

    def test_exceeds_limit_evicts_oldest(self):
        with tempfile.TemporaryDirectory() as tmp:
            _setup_topics_dir(tmp, 25)
            state = default_memory_state()
            dreamer = MemoryDreamer(state, durable_root=tmp)
            removed = dreamer._gc_durable(max_entries=MAX_DURABLE_ENTRIES_PER_TOPIC)
            assert removed == 5  # 25 - 20
            entries = _count_entries(Path(tmp))
            assert entries == MAX_DURABLE_ENTRIES_PER_TOPIC

    def test_preserves_headers(self):
        with tempfile.TemporaryDirectory() as tmp:
            _setup_topics_dir(tmp, 30)
            state = default_memory_state()
            dreamer = MemoryDreamer(state, durable_root=tmp)
            dreamer._gc_durable(max_entries=10)
            md = _topic_file(Path(tmp))
            content = md.read_text(encoding="utf-8")
            assert content.startswith("# Key Decisions")

    def test_no_topics_dir(self):
        with tempfile.TemporaryDirectory() as tmp:
            state = default_memory_state()
            dreamer = MemoryDreamer(state, durable_root=tmp)
            assert dreamer._gc_durable(max_entries=10) == 0

    def test_gc_in_run_triggers(self):
        with tempfile.TemporaryDirectory() as tmp:
            _setup_topics_dir(tmp, 25)
            state = default_memory_state()
            stats, dreamer = run_memory_dream(state, durable_root=tmp)
            assert stats["durable_gc"] == 5

    def test_max_entries_zero_disables(self):
        with tempfile.TemporaryDirectory() as tmp:
            _setup_topics_dir(tmp, 25)
            state = default_memory_state()
            dreamer = MemoryDreamer(state, durable_root=tmp)
            stats = dreamer.run(max_durable=0)  # 显式禁用 GC
            assert stats["durable_gc"] == 0


def _setup_topics_dir(root: str, entry_count: int) -> Path:
    topics = Path(root) / ".agent" / "memory" / "topics"
    topics.mkdir(parents=True, exist_ok=True)
    md = topics / "key-decisions.md"
    lines = ["# Key Decisions", ""]
    for i in range(entry_count):
        lines.append(f"- Decision {i}: use pytest for testing")
    md.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return topics


def _topic_file(topics_root: Path) -> Path:
    return topics_root / ".agent" / "memory" / "topics" / "key-decisions.md"


def _count_entries(topics_root: Path) -> int:
    md = _topic_file(topics_root)
    if not md.is_file():
        return 0
    lines = md.read_text(encoding="utf-8").splitlines()
    return sum(1 for line in lines if line.strip() and not line.startswith("#"))


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


# ---------------------------------------------------------------------------
# 晋升建议（V1.5-Bonus3）
# ---------------------------------------------------------------------------


class TestSuggestPromotions:
    def test_no_decisions_no_suggestions(self):
        state = default_memory_state()
        state["episodic_notes"] = [
            {**_make_note("obs", 1), "kind": "observation", "retrieve_count": 5},
        ]
        dreamer = MemoryDreamer(state)
        n = dreamer._suggest_promotions(hit_min=3)
        assert n == 0
        assert dreamer.promotion_hints == []

    def test_decision_below_threshold_no_suggest(self):
        state = default_memory_state()
        state["episodic_notes"] = [
            {**_make_note("dec", 1), "kind": "decision", "retrieve_count": 2},
        ]
        dreamer = MemoryDreamer(state)
        n = dreamer._suggest_promotions(hit_min=3)
        assert n == 0

    def test_decision_above_threshold_suggests(self):
        state = default_memory_state()
        state["episodic_notes"] = [
            {
                **_make_note("fix import", 1),
                "kind": "decision",
                "retrieve_count": 5,
                "source": "patcher",
            },
        ]
        dreamer = MemoryDreamer(state)
        n = dreamer._suggest_promotions(hit_min=3)
        assert n == 1
        assert len(dreamer.promotion_hints) == 1
        assert dreamer.promotion_hints[0]["text"] == "fix import"
        assert dreamer.promotion_hints[0]["retrieve_count"] == 5
        assert dreamer.promotion_hints[0]["kind"] == "decision"

    def test_multiple_decisions(self):
        state = default_memory_state()
        state["episodic_notes"] = [
            {**_make_note("d1", 1), "kind": "decision", "retrieve_count": 3},
            {**_make_note("d2", 2), "kind": "decision", "retrieve_count": 4},
            {**_make_note("obs", 3), "kind": "observation", "retrieve_count": 10},
        ]
        dreamer = MemoryDreamer(state)
        n = dreamer._suggest_promotions(hit_min=3)
        assert n == 2
        assert dreamer.stats["promotion_suggestions"] == 2

    def test_suggestions_in_run_stats(self):
        state = default_memory_state()
        state["episodic_notes"] = [
            {
                **_make_note("use pytest", 1),
                "kind": "decision",
                "retrieve_count": 4,
                "source": "patcher",
            },
        ]
        stats, dreamer = run_memory_dream(state)
        assert stats["promotion_suggestions"] == 1
        assert len(dreamer.promotion_hints) == 1


# ---------------------------------------------------------------------------
# 路由重建（V1.5-Bonus3）
# ---------------------------------------------------------------------------


class TestRebuildRoutingTable:
    def test_empty_durable_root(self):
        state = default_memory_state()
        dreamer = MemoryDreamer(state, durable_root="")
        result = dreamer._rebuild_routing_table()
        assert result == {}

    def test_counts_topic_entries(self):
        with tempfile.TemporaryDirectory() as tmp:
            _setup_topics_dir(tmp, 12)
            state = default_memory_state()
            dreamer = MemoryDreamer(state, durable_root=tmp)
            result = dreamer._rebuild_routing_table()
            assert "key-decisions" in result
            assert result["key-decisions"] == 12

    def test_routing_in_run_stats(self):
        with tempfile.TemporaryDirectory() as tmp:
            _setup_topics_dir(tmp, 8)
            state = default_memory_state()
            stats, dreamer = run_memory_dream(state, durable_root=tmp)
            assert stats["routing_entries"] == 8


# ---------------------------------------------------------------------------
# trace / health 更新（V1.5-Bonus3）
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# GC + episodic 上限（V1.5-Bonus3）
# ---------------------------------------------------------------------------


class TestTrimByTime:
    """_trim 按 created_at 淘汰最旧，保留最新。"""

    def test_within_limit_unchanged(self):
        state = default_memory_state()
        state["episodic_notes"] = [_make_note(f"n{i}", i) for i in range(5)]
        dreamer = MemoryDreamer(state)
        assert dreamer._trim() == 0

    def test_exceeds_limit_trims_oldest(self):
        state = default_memory_state()
        # 旧条目在前，新条目在后
        old = time.time() - 3600
        new = time.time()
        notes = []
        for i in range(MAX_EPISODIC_NOTES + 3):
            notes.append(_make_note(f"n{i}", i, created_at=old if i < 3 else new))
        # 打乱顺序验证排序
        import random

        random.shuffle(notes)
        state["episodic_notes"] = notes
        dreamer = MemoryDreamer(state)
        removed = dreamer._trim()
        assert removed == 3
        assert len(state["episodic_notes"]) == MAX_EPISODIC_NOTES
        # 保留的是最新的
        for n in state["episodic_notes"]:
            assert n["created_at"] >= new

    def test_all_same_time_keeps_by_position(self):
        state = default_memory_state()
        t = time.time()
        n = MAX_EPISODIC_NOTES + 5
        state["episodic_notes"] = [_make_note(f"n{i}", i, created_at=t) for i in range(n)]
        dreamer = MemoryDreamer(state)
        dreamer._trim()
        assert len(state["episodic_notes"]) == MAX_EPISODIC_NOTES


class TestDurableGCMtime:
    """Durable GC 按 mtime 淘汰旧文件条目。"""

    def test_mtime_sorted_eviction(self):
        import os
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            topics_dir = Path(tmp) / ".agent" / "memory" / "topics"
            topics_dir.mkdir(parents=True)

            # 创建两个 topic 文件，一个旧一个新
            old_file = topics_dir / "project-conventions.md"
            new_file = topics_dir / "key-decisions.md"
            old_file.write_text(
                "# Project Conventions\n" + "\n".join(f"entry {i}" for i in range(25))
            )
            new_file.write_text("# Key Decisions\n" + "\n".join(f"decision {i}" for i in range(25)))

            # 设置不同的 mtime
            old_mtime = time.time() - 86400
            new_mtime = time.time()
            os.utime(str(old_file), (old_mtime, old_mtime))
            os.utime(str(new_file), (new_mtime, new_mtime))

            state = default_memory_state()
            dreamer = MemoryDreamer(state, durable_root=tmp)
            removed = dreamer._gc_durable(max_entries=20)
            assert removed == 10  # 5 from old + 5 from new

    def test_chunked_gc_removes_oldest_chunk(self):
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            from agent_runtime.features.memory.durable import DurableMemoryStore

            store = DurableMemoryStore(tmp)
            # 写入足够多的条目触发 chunked
            entries = [("key-decisions", f"big {i}: " + "y" * 1000) for i in range(60)]
            store.promote(entries)

            chunk_dir = store.topics_dir / "key-decisions"
            chunks_before = sorted(chunk_dir.glob("chunk-*.md"))
            assert len(chunks_before) >= 2

            state = default_memory_state()
            dreamer = MemoryDreamer(state, durable_root=tmp)
            # 限制为 15 条 → 应删除最旧 chunk
            dreamer._gc_durable(max_entries=15)
            chunks_after = sorted(chunk_dir.glob("chunk-*.md"))
            # 至少删了 1 个 chunk
            assert len(chunks_after) < len(chunks_before)


class TestMemoryGCIntegration:
    """GC 集成：Dream run 末尾触发 + 条数上限。"""

    def test_trim_triggered_in_run(self):
        state = default_memory_state()
        n = MAX_EPISODIC_NOTES + 5
        state["episodic_notes"] = [_make_note(f"n{i}", i) for i in range(n)]
        stats, dreamer = run_memory_dream(state)
        assert stats["trimmed"] == 5
        assert len(state["episodic_notes"]) == MAX_EPISODIC_NOTES

    def test_gc_triggered_in_run(self):
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            _setup_topics_dir(tmp, 30)
            state = default_memory_state()
            stats, dreamer = run_memory_dream(state, durable_root=tmp)
            assert stats["durable_gc"] > 0

    def test_both_gc_and_trim_in_run(self):
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            _setup_topics_dir(tmp, 25)
            state = default_memory_state()
            n = MAX_EPISODIC_NOTES + 3
            state["episodic_notes"] = [_make_note(f"n{i}", i) for i in range(n)]
            stats, dreamer = run_memory_dream(state, durable_root=tmp)
            assert stats["trimmed"] == 3
            assert stats["durable_gc"] == 5
            assert len(state["episodic_notes"]) == MAX_EPISODIC_NOTES


class TestDreamTraceHealth:
    def test_dream_summary_includes_new_fields(self):
        trace = dream_summary_to_trace(
            {
                "deduped": 1,
                "expired": 0,
                "trimmed": 2,
                "durable_gc": 3,
                "total_before": 10,
                "total_after": 4,
                "promotion_suggestions": 2,
                "routing_entries": 15,
            }
        )
        assert trace["durable_gc"] == 3
        assert trace["promotion_suggestions"] == 2
        assert trace["routing_entries"] == 15

    def test_dream_summary_with_hints(self):
        dreamer = MemoryDreamer(default_memory_state())
        dreamer.promotion_hints = [
            {"text": "use ruff", "retrieve_count": 3, "kind": "decision"},
        ]
        stats = {"promotion_suggestions": 1}
        trace = dream_summary_to_trace(stats, dreamer)
        assert "promotion_hints" in trace
        assert len(trace["promotion_hints"]) == 1

    def test_dream_summary_without_dreamer(self):
        trace = dream_summary_to_trace({"deduped": 0})
        assert "promotion_hints" not in trace
        assert trace["promotion_suggestions"] == 0
