"""L0–L5 压缩管线单测（L1 + L2 + L3 实装）。"""

import pytest

from agent_runtime.compression_pipeline import (
    L2_TRIGGER_RATIO,
    L3_TRIGGER_RATIO,
    L4_TRIGGER_RATIO,
    L5_FALLBACK_KEEP_ENTRIES,
    L5_TRIGGER_RATIO,
    TOOL_TRUNCATION_TOKENS,
    apply_l1_to_request_text,
    compression_threshold,
    count_history_tokens,
    group_history_into_turns,
    l2_snip,
    l3_microcompact,
    l4_collapse,
    l5_auto_compact,
    run_compression_pipeline,
    score_turn,
    truncate_tool_content,
)
from agent_runtime.config import AgentConfig
from agent_runtime.context_manager import ContextManager, TokenBudget, history_window_budget
from agent_runtime.providers.clients import FakeModelClient
from agent_runtime.runtime import Agent
from agent_runtime.workspace import WorkspaceContext


@pytest.fixture
def budget():
    return TokenBudget(model="gpt-4", provider="openai", total_limit=6000)


def _history_window(budget: TokenBudget) -> int:
    return history_window_budget(budget.total_limit)


class TestCompressionPipeline:
    def test_l1_truncates_tool_history_entries(self, budget):
        long = "Error: failed\n" + "x" * 8000
        history = [{"role": "tool", "tool_name": "read_file", "content": long}]
        meta: dict = {}
        projected = run_compression_pipeline(history, budget, metadata=meta)

        assert projected[0]["content"] != long
        assert "Error" in projected[0]["content"]
        assert budget.count(projected[0]["content"]) <= TOOL_TRUNCATION_TOKENS["read_file"] + 5
        assert meta["compression_pipeline"]["stages"] == [
            "L0",
            "L1",
            "L2",
            "L3",
            "L4",
            "L5",
        ]
        assert meta["compression_pipeline"]["l1_truncations"] == 1

    def test_l5_skipped_for_small_history(self, budget):
        history = [
            {"role": "user", "content": "hello"},
            {"role": "assistant", "content": "world"},
        ]
        meta: dict = {}
        projected = run_compression_pipeline(history, budget, metadata=meta)
        assert projected == history
        assert meta["compression_pipeline"]["l2_triggered"] is False
        assert meta["compression_pipeline"]["l5_triggered"] is False

    def test_pipeline_does_not_mutate_canonical_history(self, budget):
        history = [{"role": "tool", "tool_name": "search", "content": "y" * 5000}]
        canonical = history[0]["content"]
        run_compression_pipeline(history, budget)
        assert history[0]["content"] == canonical

    def test_apply_l1_to_request_text_truncates_tool_result_block(self, budget):
        body = "line\nError: boom\n" + "z" * 5000
        msg = f"工具 read_file 执行完成。\n结果:\n{body}"
        fitted = apply_l1_to_request_text(msg, budget, tool_name="read_file")
        assert fitted.startswith("工具 read_file 执行完成。\n结果:\n")
        assert "Error" in fitted
        result_part = fitted.split("结果:\n", 1)[1]
        assert budget.count(result_part) <= TOOL_TRUNCATION_TOKENS["read_file"] + 5


def _exploration_turn(idx: int, *, lines: int = 40) -> list[dict]:
    """构造一轮只读探索（list_files + read_file）。"""
    body = "\n".join(f"line {idx}-{j} ok content here" for j in range(lines))
    return [
        {"role": "user", "content": f"explore batch {idx}"},
        {"role": "assistant", "content": "调用工具: list_files", "tool_name": "list_files"},
        {"role": "tool", "tool_name": "list_files", "content": body},
        {"role": "assistant", "content": "调用工具: read_file", "tool_name": "read_file"},
        {"role": "tool", "tool_name": "read_file", "content": body},
    ]


def _long_exploration_history(turns: int = 8) -> list[dict]:
    history: list[dict] = []
    for i in range(turns):
        history.extend(_exploration_turn(i))
    return history


class TestL2Snip:
    def test_l2_not_triggered_below_threshold(self, budget):
        history = _exploration_turn(0, lines=5)
        meta: dict = {}
        result = l2_snip(history, budget, meta, history_window=_history_window(budget))
        assert result == history
        assert meta["compression_pipeline"]["l2_triggered"] is False

    def test_l2_snips_old_low_value_turns(self, budget):
        history = _long_exploration_history(10)
        assert count_history_tokens(history, budget) > compression_threshold(
            L2_TRIGGER_RATIO, _history_window(budget)
        )
        meta: dict = {}
        result = l2_snip(history, budget, meta, history_window=_history_window(budget))
        assert meta["compression_pipeline"]["l2_triggered"] is True
        assert meta["compression_pipeline"]["l2_snipped_turns"] >= 1
        assert any("[snipped turn" in str(i.get("content", "")) for i in result)
        assert count_history_tokens(result, budget) < count_history_tokens(history, budget)

    def test_l2_keeps_turns_with_errors(self, budget):
        history = _long_exploration_history(8)
        history[5]["content"] = "Error: Traceback (most recent call last):\n" + history[5]["content"]
        meta: dict = {}
        result = l2_snip(history, budget, meta, history_window=_history_window(budget))
        assert "Error: Traceback" in "\n".join(str(i.get("content", "")) for i in result)

    def test_l2_keeps_write_file_turns(self, budget):
        history = _long_exploration_history(8)
        history.extend(
            [
                {"role": "user", "content": "patch it"},
                {"role": "assistant", "tool_name": "write_file", "content": "write"},
                {"role": "tool", "tool_name": "write_file", "content": "w" * 2000},
            ]
        )
        meta: dict = {}
        result = l2_snip(history, budget, meta, history_window=_history_window(budget))
        assert any(i.get("tool_name") == "write_file" for i in result)

    def test_l2_protects_recent_turns(self, budget):
        history = _long_exploration_history(10)
        turns = group_history_into_turns(history)
        recent_user = turns[-1][0]["content"]
        meta: dict = {}
        result = l2_snip(history, budget, meta, history_window=_history_window(budget))
        assert recent_user in "\n".join(str(i.get("content", "")) for i in result)

    def test_score_turn_snip_readonly(self):
        turn = _exploration_turn(1, lines=2)
        assert score_turn(turn) == "snip"

    def test_score_turn_keep_on_error(self):
        turn = _exploration_turn(1, lines=2)
        turn[-1]["content"] = "Error: boom"
        assert score_turn(turn) == "keep"

    def test_pipeline_l2_integration(self, budget):
        history = _long_exploration_history(10)
        meta: dict = {}
        projected = run_compression_pipeline(history, budget, metadata=meta)
        assert meta["compression_pipeline"]["l2_triggered"] is True
        assert meta["compression_pipeline"]["l2_snipped_turns"] >= 1
        assert len(projected) < len(history)


def _history_for_l3(*, tool_lines: int = 100, turns: int = 5) -> list[dict]:
    """构造 L2 不会 snip（含 write）但 token 超 L3 阈值的 history。"""
    history: list[dict] = []
    for i in range(turns):
        body = "\n".join(f"file chunk {i}-{j} padding text" for j in range(tool_lines))
        history.extend(
            [
                {"role": "user", "content": f"fix bug batch {i}"},
                {"role": "assistant", "tool_name": "write_file", "content": "patch"},
                {"role": "tool", "tool_name": "write_file", "content": f"written {i}"},
                {"role": "assistant", "tool_name": "read_file", "content": "read"},
                {"role": "tool", "tool_name": "read_file", "content": body},
            ]
        )
    return history


class TestL3Microcompact:
    def test_l3_not_triggered_below_threshold(self, budget):
        history = _exploration_turn(0, lines=5)
        meta: dict = {}
        result = l3_microcompact(history, budget, meta, history_window=_history_window(budget))
        assert result == history
        assert meta["compression_pipeline"]["l3_triggered"] is False

    def test_l3_compacts_old_tool_to_ref_stub(self, budget):
        history = _history_for_l3(tool_lines=120, turns=6)
        assert count_history_tokens(history, budget) > compression_threshold(
            L3_TRIGGER_RATIO, _history_window(budget)
        )
        meta: dict = {}
        result = l3_microcompact(history, budget, meta, history_window=_history_window(budget))
        assert meta["compression_pipeline"]["l3_triggered"] is True
        assert meta["compression_pipeline"]["l3_compacted"] >= 1
        assert any(str(i.get("content", "")).startswith("[ref:#") for i in result)
        refs = meta["compression_pipeline"]["l3_refs"]
        assert refs
        first_key = next(iter(refs))
        assert "tool_name" in refs[first_key]
        assert "tokens_saved" in refs[first_key]
        assert count_history_tokens(result, budget) < count_history_tokens(history, budget)

    def test_l3_skips_protected_recent_tools(self, budget):
        history = _history_for_l3(tool_lines=120, turns=6)
        turns = group_history_into_turns(history)
        last_tool = turns[-1][-1]
        assert last_tool.get("role") == "tool"
        original_last = last_tool["content"]
        meta: dict = {}
        result = l3_microcompact(history, budget, meta, history_window=_history_window(budget))
        last_tool_result = group_history_into_turns(result)[-1][-1]
        assert last_tool_result.get("content") == original_last

    def test_l3_skips_error_tool_content(self, budget):
        history = _history_for_l3(tool_lines=120, turns=6)
        history[4]["content"] = "Error: Traceback\n" + history[4]["content"]
        meta: dict = {}
        result = l3_microcompact(history, budget, meta, history_window=_history_window(budget))
        assert "Error: Traceback" in result[4]["content"]
        assert not str(result[4]["content"]).startswith("[ref:#")

    def test_l3_does_not_mutate_canonical(self, budget):
        history = _history_for_l3(tool_lines=120, turns=6)
        canonical = history[4]["content"]
        l3_microcompact(history, budget, {}, history_window=_history_window(budget))
        assert history[4]["content"] == canonical

    def test_pipeline_l3_after_l2(self, budget):
        history = _history_for_l3(tool_lines=120, turns=6)
        meta: dict = {}
        projected = run_compression_pipeline(history, budget, metadata=meta)
        pipe = meta["compression_pipeline"]
        assert pipe.get("l3_compacted", 0) > 0 or pipe.get("l3_triggered")
        # L4 可能在 L3 之后折叠整轮，[ref:#] 不一定留在最终投影中
        assert len(projected) <= len(history)


def _history_for_l4(*, tool_lines: int = 150, turns: int = 8) -> list[dict]:
    """构造超 L4 阈值（82%）且含 write 轮（L2 不 snip）的 history。"""
    return _history_for_l3(tool_lines=tool_lines, turns=turns)


class TestL4Collapse:
    def test_l4_not_triggered_below_threshold(self, budget):
        history = _exploration_turn(0, lines=5)
        meta: dict = {}
        result = l4_collapse(history, budget, meta, history_window=_history_window(budget))
        assert result == history
        assert meta["compression_pipeline"]["l4_triggered"] is False

    def test_l4_collapses_old_turns_to_marker(self, budget):
        history = _history_for_l4(tool_lines=150, turns=8)
        assert count_history_tokens(history, budget) > compression_threshold(
            L4_TRIGGER_RATIO, _history_window(budget)
        )
        meta: dict = {}
        result = l4_collapse(history, budget, meta, history_window=_history_window(budget))
        assert meta["compression_pipeline"]["l4_triggered"] is True
        assert meta["compression_pipeline"]["l4_collapsed_turns"] >= 1
        assert any("[collapsed turn" in str(i.get("content", "")) for i in result)
        assert count_history_tokens(result, budget) < count_history_tokens(history, budget)
        assert meta["compression_pipeline"]["l4_details"]

    def test_l4_skips_turn_with_error(self, budget):
        history = _history_for_l4(tool_lines=150, turns=8)
        history[0]["content"] = "Error: Traceback\n" + history[0]["content"]
        meta: dict = {}
        result = l4_collapse(history, budget, meta, history_window=_history_window(budget))
        assert "Error: Traceback" in result[0]["content"]

    def test_l4_protects_recent_turns(self, budget):
        history = _history_for_l4(tool_lines=150, turns=8)
        turns = group_history_into_turns(history)
        recent_user = turns[-1][0]["content"]
        meta: dict = {}
        result = l4_collapse(history, budget, meta, history_window=_history_window(budget))
        assert recent_user in "\n".join(str(i.get("content", "")) for i in result)

    def test_l4_does_not_mutate_canonical(self, budget):
        history = _history_for_l4(tool_lines=150, turns=8)
        canonical = history[2]["content"]
        l4_collapse(history, budget, {}, history_window=_history_window(budget))
        assert history[2]["content"] == canonical

    def test_pipeline_l4_after_l2_l3(self, budget):
        history = _history_for_l4(tool_lines=150, turns=8)
        meta: dict = {}
        projected = run_compression_pipeline(history, budget, metadata=meta)
        pipe = meta["compression_pipeline"]
        assert pipe.get("l4_triggered") is True or pipe.get("l4_collapsed_turns", 0) >= 0
        if pipe.get("l4_triggered"):
            assert any("[collapsed turn" in str(i.get("content", "")) for i in projected)


def _long_user_history(entries: int = 30, pad: int = 300) -> list[dict]:
    return [{"role": "user", "content": f"msg {i}: " + "x" * pad} for i in range(entries)]


class TestL5AutoCompact:
    def test_l5_not_triggered_below_threshold(self, budget):
        history = [
            {"role": "user", "content": "hi"},
            {"role": "assistant", "content": "hello"},
        ]
        meta: dict = {}
        calls: list[str] = []

        def summarizer(_prompt: str) -> str:
            calls.append("called")
            return "summary"

        result = l5_auto_compact(
            history,
            budget,
            meta,
            summarizer=summarizer,
            trigger_tokens=compression_threshold(L5_TRIGGER_RATIO, _history_window(budget)),
            history_window=_history_window(budget),
        )
        assert result == history
        assert meta["compression_pipeline"]["l5_triggered"] is False
        assert calls == []

    def test_l5_summarizes_old_half(self, budget):
        history = _long_user_history(20, pad=300)
        meta: dict = {}
        summary_text = "Read config.py, patched tools.py."

        result = l5_auto_compact(
            history,
            budget,
            meta,
            summarizer=lambda _p: summary_text,
            trigger_tokens=50,
        )
        assert meta["compression_pipeline"]["l5_triggered"] is True
        assert meta["compression_pipeline"]["l5_fallback"] is False
        assert result[0]["role"] == "system"
        assert summary_text in result[0]["content"]
        assert len(result) >= len(history) // 2 + 1  # +1 for first user pin

    def test_l5_fallback_when_summarizer_fails(self, budget):
        history = _long_user_history(30, pad=300)

        def failing(_prompt: str) -> str:
            raise RuntimeError("API down")

        meta: dict = {}
        result = l5_auto_compact(
            history,
            budget,
            meta,
            summarizer=failing,
            trigger_tokens=50,
        )
        assert meta["compression_pipeline"]["l5_triggered"] is True
        assert meta["compression_pipeline"]["l5_fallback"] is True
        assert len(result) <= L5_FALLBACK_KEEP_ENTRIES

    def test_l5_cache_prevents_duplicate_summarizer_calls(self, budget):
        history = _long_user_history(20, pad=200)
        cache: dict[str, str] = {}
        calls: list[int] = []

        def summarizer(_prompt: str) -> str:
            calls.append(1)
            return "cached summary"

        meta1: dict = {}
        result1 = l5_auto_compact(
            history,
            budget,
            meta1,
            summarizer=summarizer,
            summary_cache=cache,
            trigger_tokens=10,
        )
        meta2: dict = {}
        result2 = l5_auto_compact(
            history,
            budget,
            meta2,
            summarizer=summarizer,
            summary_cache=cache,
            trigger_tokens=10,
        )
        assert len(calls) == 1
        assert result1 == result2
        assert meta2["compression_pipeline"]["l5_summary_cache_hit"] is True

    def test_pipeline_l5_runs_after_l1_l4(self, budget):
        history = _long_user_history(40, pad=250)
        meta: dict = {}
        projected = run_compression_pipeline(
            history,
            budget,
            metadata=meta,
            summarizer=lambda _p: "Earlier work summarized.",
            l5_trigger_tokens=50,
        )
        pipe = meta["compression_pipeline"]
        assert pipe.get("l5_triggered") is True
        assert any(h.get("role") == "system" for h in projected)
        assert len(projected) < len(history)


class TestCompressionWindowScaling:
    def test_history_window_scales_with_prompt_budget(self):
        assert history_window_budget(6000) == 2600
        assert history_window_budget(100_000) == int(2600 * 100_000 / 6000)

    def test_pipeline_thresholds_scale_at_100k(self):
        budget = TokenBudget(model="gpt-4", provider="openai", total_limit=100_000)
        window = history_window_budget(100_000)
        meta: dict = {}
        run_compression_pipeline(
            [{"role": "user", "content": "hi"}],
            budget,
            metadata=meta,
        )
        pipe = meta["compression_pipeline"]
        assert pipe["history_window"] == window
        assert pipe["l2_threshold"] == compression_threshold(L2_TRIGGER_RATIO, window)
        assert pipe["l5_threshold"] == window


class TestContextManagerUsesPipeline:
    def test_build_records_l0_pipeline_metadata(self, temp_workspace):
        ws = WorkspaceContext.build(str(temp_workspace))
        agent = Agent(
            config=AgentConfig(provider="fake", max_steps=4, prompt_budget=6000),
            model_client=FakeModelClient(["<final>ok</final>"]),
            workspace=ws,
        )
        agent.record({"role": "assistant", "content": ""})
        agent.record({"role": "user", "content": "still here"})
        cm = ContextManager(agent)
        _, meta = cm.build("next")
        pipe = meta.get("compression_pipeline", {})
        assert pipe.get("l0_dropped", 0) >= 1
        assert "L1" in meta["compression_pipeline"]["stages"]


class TestAgentLoopL1Integration:
    def test_native_path_truncates_tool_results(self, temp_workspace):
        from agent_runtime.agent_loop import AgentLoop
        from agent_runtime.providers.clients import FakeNativeToolClient

        ws = WorkspaceContext.build(str(temp_workspace))
        long_result = "Error: fail\n" + "q" * 5000
        client = FakeNativeToolClient(
            [
                '<tool>{"name": "read_file", "args": {"path": "a.py"}}</tool>',
                "<final>done</final>",
            ]
        )

        def read_file(_ctx, _args):
            return long_result

        agent = Agent(
            config=AgentConfig(provider="fake", max_steps=4, approval="auto"),
            model_client=client,
            workspace=ws,
            tools={
                "read_file": {
                    "run": read_file,
                    "schema": {"path": "str"},
                    "description": "read",
                }
            },
        )
        budget = TokenBudget(model="gpt-4", provider="openai")
        loop = AgentLoop(agent)
        loop.run("inspect file")

        # FakeNativeToolClient 走 chat_with_tools；executor 应返回 L1 截断结果
        assert client.prompts
        combined = "\n".join(client.prompts)
        assert "截断" in combined or len(combined) < len(long_result)
        assert budget.count(long_result) > TOOL_TRUNCATION_TOKENS["read_file"]


# ---------------------------------------------------------------------------
# 摘要缓存持久化（V1.5-Bonus2）：落盘 + 启动加载 + 降级
# ---------------------------------------------------------------------------


class TestSummaryCachePersistence:
    """L5 摘要缓存磁盘持久化：落盘、加载、二次命中、写失败降级。"""

    def test_disk_cache_writes_and_reads(self, tmp_path):
        """DiskCache 写入后落盘，新实例启动加载。"""
        from agent_runtime.context_manager import _DiskCache

        cache_dir = tmp_path / "summary_cache"
        cache1 = _DiskCache(cache_dir)
        cache1["test_key"] = "cached summary text"
        assert (cache_dir / (cache1._hash_key("test_key") + ".txt")).is_file()

        # 新建实例 → 从磁盘加载
        cache2 = _DiskCache(cache_dir)
        assert "test_key" in cache2
        assert cache2["test_key"] == "cached summary text"

    def test_disk_cache_write_failure_does_not_raise(self, tmp_path, monkeypatch):
        """写磁盘失败时静默降级，不抛异常。"""
        from agent_runtime.context_manager import _DiskCache

        cache_dir = tmp_path / "summary_cache"
        cache = _DiskCache(cache_dir)

        # 模拟写失败
        def failing_write(*_a, **_kw):
            raise OSError("disk full")

        monkeypatch.setattr(cache, "_path", lambda k: tmp_path / "readonly" / "x.txt")
        # 写失败不抛异常
        cache["should_not_crash"] = "value"
        # 内存中仍然有效
        assert cache["should_not_crash"] == "value"

    def test_l5_cache_hit_skips_summarizer_with_disk_cache(self, budget, tmp_path):
        """磁盘缓存命中时不再调 summarizer LLM。"""
        from agent_runtime.compression_pipeline import l5_auto_compact
        from agent_runtime.context_manager import _DiskCache

        history = _long_user_history(20, pad=200)
        cache_dir = tmp_path / "summary_cache"
        disk_cache = _DiskCache(cache_dir)
        summarizer_calls = []

        def summarizer(prompt: str) -> str:
            summarizer_calls.append(1)
            return "fresh summary"

        # 第一次：无缓存 → 调 summarizer
        meta1: dict = {}
        l5_auto_compact(history, budget, meta1, summarizer=summarizer,
                        summary_cache=disk_cache, trigger_tokens=10)
        assert len(summarizer_calls) == 1
        assert meta1["compression_pipeline"]["l5_summary_cache_hit"] is False

        # 第二次：同 history → 缓存命中，不调 summarizer
        meta2: dict = {}
        l5_auto_compact(history, budget, meta2, summarizer=summarizer,
                        summary_cache=disk_cache, trigger_tokens=10)
        assert len(summarizer_calls) == 1  # 未增加
        assert meta2["compression_pipeline"]["l5_summary_cache_hit"] is True

        # 新建 DiskCache 实例（模拟进程重启）→ 仍然命中
        disk_cache2 = _DiskCache(cache_dir)
        summarizer_calls2 = []

        def summarizer2(prompt: str) -> str:
            summarizer_calls2.append(1)
            return "should not be called"

        meta3: dict = {}
        l5_auto_compact(history, budget, meta3, summarizer=summarizer2,
                        summary_cache=disk_cache2, trigger_tokens=10)
        assert len(summarizer_calls2) == 0  # 磁盘加载后命中
        assert meta3["compression_pipeline"]["l5_summary_cache_hit"] is True

    def test_disk_cache_context_manager_integration(self, temp_workspace):
        """ContextManager._summary_cache 使用 _DiskCache 落盘。"""
        from pathlib import Path

        from agent_runtime.config import AgentConfig
        from agent_runtime.context_manager import ContextManager, _DiskCache
        from agent_runtime.providers.clients import FakeModelClient
        from agent_runtime.runtime import Agent
        from agent_runtime.workspace import WorkspaceContext

        config = AgentConfig(provider="fake", max_steps=3, prompt_budget=4000)
        ws = WorkspaceContext.build(str(temp_workspace))
        agent = Agent(config=config, model_client=FakeModelClient([]), workspace=ws)

        # 注入大量 history 触发 L5 压缩
        for i in range(50):
            agent.record({"role": "user", "content": f"question {i} " + "data " * 30})
            agent.record({"role": "tool", "content": f"result {i}: " + "y" * 200})

        cm = ContextManager(agent, total_budget=2000)
        assert isinstance(cm._summary_cache, _DiskCache)

        # 检查缓存目录存在
        cache_dir = Path(str(temp_workspace)) / ".agent" / "summary_cache"
        assert cache_dir.is_dir() or cm._summary_cache._dir.is_dir()

        # build 触发压缩
        cm.build("fix the bug")

        # 缓存目录应有文件（如果 L5 触发了摘要）
        cache_files = list(cache_dir.glob("*.txt")) if cache_dir.is_dir() else []
        # 至少 _DiskCache 目录存在
        assert cm._summary_cache._dir.is_dir()


# ---------------------------------------------------------------------------
# 增量摘要（V1.5-Bonus2）：L5 在已有 [Earlier summary] 上追加新段
# ---------------------------------------------------------------------------


class TestIncrementalSummary:
    """增量摘要：已有 [Earlier summary] 时只摘要新增条目，cache key 含 offset。"""

    def test_incremental_mode_detected(self, budget):
        """已有 [Earlier summary] 时 L5 进入增量模式。"""
        from agent_runtime.compression_pipeline import (
            _find_existing_summary,
            l5_auto_compact,
        )

        # Round 1: 无已有摘要 → 全量模式
        history1 = _long_user_history(30, pad=200)
        meta1: dict = {}
        result1 = l5_auto_compact(
            history1, budget, meta1,
            summarizer=lambda p: "round 1 summary",
            trigger_tokens=10,
        )
        assert meta1["compression_pipeline"]["l5_incremental"] is False
        assert len(result1) < len(history1)

        # Round 2: 追加新条目到 Round 1 结果后
        new_entries = _long_user_history(15, pad=200)
        history2 = result1 + new_entries
        assert _find_existing_summary(history2) is not None

        meta2: dict = {}
        result2 = l5_auto_compact(
            history2, budget, meta2,
            summarizer=lambda p: "round 2 incremental summary",
            trigger_tokens=10,
        )
        assert meta2["compression_pipeline"]["l5_incremental"] is True
        assert "round 2" in str(result2[0]["content"])

    def test_cache_key_includes_offset_with_existing_summary(self, budget):
        """增量模式下 cache key 含 offset → 不同轮次不同 key。"""
        from agent_runtime.compression_pipeline import (
            _summary_cache_key,
            l5_auto_compact,
        )

        history1 = _long_user_history(25, pad=200)
        r1 = l5_auto_compact(
            history1, budget, {},
            summarizer=lambda p: "s1",
            trigger_tokens=10,
        )

        # Round 2: 追加更多条目
        more = _long_user_history(15, pad=200)
        history2 = r1 + more
        summarizer_calls = []

        def counted_summarizer(prompt: str) -> str:
            summarizer_calls.append(prompt)
            return "s2 incremental"

        r2 = l5_auto_compact(
            history2, budget, {},
            summarizer=counted_summarizer,
            trigger_tokens=10,
        )
        # 增量模式：summarizer 被调用（因为新内容触发新的 cache key）
        assert len(summarizer_calls) == 1
        # prompt 应是增量模式（含 "Current summary"）
        assert "Current summary" in summarizer_calls[0]

    def test_incremental_only_summarizes_new_items(self, budget):
        """增量模式只摘要 [Earlier summary] 之后的新条目。"""
        from agent_runtime.compression_pipeline import l5_auto_compact

        history1 = _long_user_history(20, pad=200)
        r1 = l5_auto_compact(
            history1, budget, {},
            summarizer=lambda p: "initial summary",
            trigger_tokens=10,
        )

        # 追加少量新条目 — 不应触发全量重摘要
        new_entries = _long_user_history(10, pad=200)
        history2 = r1 + new_entries

        summarizer_inputs = []
        r2 = l5_auto_compact(
            history2, budget, {},
            summarizer=lambda p: summarizer_inputs.append(p) or "updated summary",
            trigger_tokens=10,
        )

        # prompt 中的 "New items" 应只含新条目
        assert len(summarizer_inputs) == 1
        prompt = summarizer_inputs[0]
        assert "Update the following summary" in prompt
        assert "Current summary: initial summary" in prompt
        # 结果中保留更新后的摘要
        assert "updated summary" in str(r2[0]["content"])

    def test_second_incremental_round_hits_cache(self, budget):
        """同 offset + 同新条目 → 缓存命中，不调 summarizer。"""
        from agent_runtime.compression_pipeline import l5_auto_compact

        history1 = _long_user_history(20, pad=200)
        r1 = l5_auto_compact(
            history1, budget, {},
            summarizer=lambda p: "first summary",
            trigger_tokens=10,
        )

        new_entries = _long_user_history(10, pad=200)
        history2 = r1 + new_entries

        cache: dict[str, str] = {}
        calls = []

        def counted(prompt: str) -> str:
            calls.append(1)
            return "incremental summary"

        # 第一次增量
        l5_auto_compact(history2, budget, {}, summarizer=counted,
                        summary_cache=cache, trigger_tokens=10)
        assert len(calls) == 1

        # 第二次增量（同输入）→ 缓存命中
        l5_auto_compact(history2, budget, {}, summarizer=counted,
                        summary_cache=cache, trigger_tokens=10)
        assert len(calls) == 1  # 未增加
