"""Durable Memory 单测：读写 Markdown 文件、promote、过滤。"""

import tempfile
import time

import pytest

from agent_runtime.features.memory import (
    DurableMemoryStore,
    _extract_promotions,
    _has_save_intent,
    promote_durable_memory,
    reject_durable_reason,
)


@pytest.fixture
def store():
    """创建临时目录下的 DurableMemoryStore。"""
    with tempfile.TemporaryDirectory() as tmpdir:
        s = DurableMemoryStore(root=tmpdir)
        yield s


class TestDurableMemoryStore:
    """DurableMemoryStore 读写测试。"""

    def test_promote_writes_file(self, store):
        store.promote([("user-preferences", "Preference: use pytest for testing")])
        topic_file = store.topics_dir / "user-preferences.md"
        assert topic_file.exists()
        content = topic_file.read_text()
        assert "pytest" in content

    def test_promote_multiple_topics(self, store):
        store.promote(
            [
                ("user-preferences", "Preference: default timeout is 30s"),
                ("key-decisions", "Decision: use urllib instead of requests"),
            ]
        )
        assert (store.topics_dir / "user-preferences.md").exists()
        assert (store.topics_dir / "key-decisions.md").exists()

    def test_upsert_replaces_same_first_line(self, store):
        """首行完全相同时替换，首行不同时新增。"""
        store.promote([("user-preferences", "Preference: use pytest")])
        store.promote([("user-preferences", "Preference: use pytest")])  # 完全相同 → 替换
        entries = store._read_topic(store.topics_dir / "user-preferences.md")
        assert len(entries) == 1
        assert "pytest" in entries[0]

    def test_upsert_different_first_line_adds(self, store):
        """首行不同时新增条目。"""
        store.promote([("user-preferences", "Preference: use pytest")])
        store.promote([("user-preferences", "Preference: use pytest + coverage")])
        entries = store._read_topic(store.topics_dir / "user-preferences.md")
        assert len(entries) == 2

    def test_upsert_new_subject_adds(self, store):
        store.promote([("user-preferences", "Preference: use pytest")])
        store.promote([("user-preferences", "Preference: line width is 100")])
        entries = store._read_topic(store.topics_dir / "user-preferences.md")
        assert len(entries) == 2

    def test_retrieval_finds_match(self, store):
        store.promote([("key-decisions", "Decision: use tiktoken for token counting")])
        results = store.retrieval("tiktoken")
        assert len(results) == 1
        assert "tiktoken" in results[0]["text"]
        assert results[0]["topic"] == "key-decisions"

    def test_retrieval_no_match(self, store):
        store.promote([("key-decisions", "Decision: use tiktoken")])
        results = store.retrieval("nonexistent")
        assert results == []

    def test_index_updated(self, store):
        store.promote([("user-preferences", "Preference: test")])
        index_path = store.memory_dir / "MEMORY.md"
        assert index_path.exists()
        content = index_path.read_text()
        assert "user-preferences" in content


class TestPromoteDurableMemory:
    """promote_durable_memory 意图检测 + 提取测试。"""

    def test_detects_remember_intent(self):
        assert _has_save_intent("remember the default test runner is pytest")
        assert _has_save_intent("请记住默认测试工具是 pytest")
        assert not _has_save_intent("what does config.py do?")

    def test_extract_preference(self):
        answer = "好的。\nPreference: use pytest for testing\n我已经记住了。"
        result = _extract_promotions(answer)
        assert len(result) == 1
        assert result[0][0] == "user-preferences"

    def test_extract_multiple_prefixes(self):
        answer = (
            "Convention: use ruff for linting\n"
            "Decision: store tokens per section\n"
            "Preference: approval=auto in CI"
        )
        result = _extract_promotions(answer)
        assert len(result) == 3

    def test_full_promote_flow(self, store):
        ok = promote_durable_memory(
            "remember default test runner is pytest",
            "我会记住。\nPreference: test runner is pytest",
            store=store,
        )
        assert ok is True
        results = store.retrieval("test runner")
        assert len(results) == 1


class TestRejectDurableReason:
    """过滤规则测试。"""

    def test_reject_empty(self):
        assert reject_durable_reason("")

    def test_reject_too_short(self):
        assert reject_durable_reason("ab")

    def test_reject_api_key(self):
        assert reject_durable_reason("sk-abcdefghijklmnopqrstuvwxyz123456")

    def test_reject_github_token(self):
        assert reject_durable_reason("ghp_abcdefghijklmnopqrstuvwxyz")

    def test_accept_valid(self):
        assert reject_durable_reason("Preference: use pytest") == ""


# ---------------------------------------------------------------------------
# 路由表升级（V1.5-Bonus3）
# ---------------------------------------------------------------------------


class TestRoutingTable:
    @pytest.fixture
    def store(self, tmp_path):
        (tmp_path / ".agent" / "memory" / "topics").mkdir(parents=True)
        return DurableMemoryStore(str(tmp_path))

    def test_index_writes_routing_table_format(self, store):
        """MEMORY.md 含路由表列: | topic | entries | bytes | strategy |。"""
        store.promote([("project-conventions", "use pytest")])
        index = (store.memory_dir / "MEMORY.md").read_text(encoding="utf-8")
        assert "| topic | entries | bytes | strategy |" in index
        assert "| project-conventions " in index
        assert "| inline |" in index

    def test_load_routing_table_parses_correctly(self, store):
        """_load_routing_table 正确解析 topic/entries/bytes/strategy。"""
        store.promote([("key-decisions", "fix type error"), ("key-decisions", "add validation")])
        routing = store._load_routing_table()
        assert "key-decisions" in routing
        assert routing["key-decisions"]["entries"] == 2
        assert routing["key-decisions"]["strategy"] == "inline"
        assert routing["key-decisions"]["bytes"] > 0

    def test_small_topic_uses_inline_strategy(self, store):
        """小 topic（<32KB）使用 inline 策略。"""
        for i in range(5):
            store.promote([("project-conventions", f"rule {i}: use standard format")])
        assert store._topic_strategy("project-conventions") == "inline"
        path = store._topic_path("project-conventions")
        assert path.is_file()

    def test_large_topic_splits_to_chunked(self, store):
        """大 topic（>32KB）自动拆分为 chunked 存储。"""
        # 写足够多的大条目触发 chunked（每条 ~1000 bytes）
        entries = [(f"key-decisions", f"decision {i}: " + "y" * 1000) for i in range(60)]
        store.promote(entries)
        strategy = store._topic_strategy("key-decisions")
        assert strategy == "chunked", (
            f"expected chunked, file size={store._topic_path('key-decisions').stat().st_size if store._topic_path('key-decisions').is_file() else 'no file'}"
        )
        # chunk 目录存在
        chunk_dir = store.topics_dir / "key-decisions"
        assert chunk_dir.is_dir()
        chunks = sorted(chunk_dir.glob("chunk-*.md"))
        assert len(chunks) >= 2

    def test_read_chunked_first_limits_chunks(self, store):
        """_read_chunked_first 只读前 N chunk。"""
        entries = [(f"key-decisions", f"decision {i}: " + "y" * 800) for i in range(80)]
        store.promote(entries)
        # 全部条目数
        all_count = len(store._read_chunked("key-decisions"))
        # 前 2 chunk 条目数 < 全部
        first_count = len(store._read_chunked_first("key-decisions", max_chunks=2))
        assert first_count < all_count
        assert first_count > 0

    def test_retrieval_uses_routing_table(self, store):
        """retrieval 先读路由表再取内容。"""
        store.promote([("project-conventions", "use ruff format for linting")])
        results = store.retrieval("ruff")
        assert len(results) >= 1
        assert results[0]["topic"] == "project-conventions"
        assert "ruff" in results[0]["text"]

    def test_chunked_retrieval_reads_limited_chunks(self, store):
        """chunked topic retrieval 只读前 2 chunk。"""
        entries = [(f"dependency-facts", f"dep {i}: pytest==" + "z" * 200) for i in range(80)]
        # 在其中加一个可检索条目
        entries[3] = ("dependency-facts", "dep 3: pytest==7.0.0 findme_marker")
        store.promote(entries)
        results = store.retrieval("findme_marker")
        assert len(results) >= 1
        assert "findme_marker" in results[0]["text"]

    def test_roundtrip_inline_to_chunked(self, store):
        """inline → chunked 转换正确保留所有条目。"""
        # 先写少量
        store.promote([("key-decisions", f"small {i}") for i in range(3)])
        inline_entries = store._read_topic("key-decisions")
        assert len(inline_entries) == 3

        # 再写到超过阈值 → chunked
        big = [(f"key-decisions", f"big {i}: " + "x" * 800) for i in range(80)]
        store.promote(big)
        chunked_entries = store._read_topic("key-decisions", strategy="chunked")
        # chunked 应包含原有 + 新条目
        assert len(chunked_entries) > 3


# ---------------------------------------------------------------------------
# 冲突状态机（V1.5-Bonus3）
# ---------------------------------------------------------------------------


class TestConflictResolution:
    """ConflictResolution 枚举 + _resolve_conflict 全部 4 状态。"""

    def test_none_when_empty_existing(self):
        from agent_runtime.features.memory.durable import ConflictResolution, _resolve_conflict

        result = _resolve_conflict("", "new entry")
        assert result == ConflictResolution.NONE

    def test_equivalent_when_content_matches(self):
        from agent_runtime.features.memory.durable import ConflictResolution, _resolve_conflict

        result = _resolve_conflict("use pytest for testing", "use pytest for testing")
        assert result == ConflictResolution.EQUIVALENT

    def test_equivalent_case_insensitive(self):
        from agent_runtime.features.memory.durable import ConflictResolution, _resolve_conflict

        result = _resolve_conflict("Use Pytest", "use pytest")
        assert result == ConflictResolution.EQUIVALENT

    def test_override_when_higher_authority(self):
        """高权威（agent）可覆盖低权威（auto）。"""
        from agent_runtime.features.memory.durable import ConflictResolution, _resolve_conflict

        existing = "old decision [authority:auto]"
        new = "new decision [authority:agent]"
        result = _resolve_conflict(existing, new, new_authority="agent")
        assert result == ConflictResolution.OVERRIDE

    def test_override_user_over_agent(self):
        """user 权威可覆盖 agent。"""
        from agent_runtime.features.memory.durable import ConflictResolution, _resolve_conflict

        existing = "agent decision [authority:agent]"
        new = "user decision [authority:user]"
        result = _resolve_conflict(existing, new, new_authority="user")
        assert result == ConflictResolution.OVERRIDE

    def test_invalid_when_lower_authority(self):
        """低权威（auto）不可覆盖高权威（agent）。"""
        from agent_runtime.features.memory.durable import ConflictResolution, _resolve_conflict

        existing = "agent decision [authority:agent]"
        new = "auto decision"
        result = _resolve_conflict(existing, new, new_authority="auto")
        assert result == ConflictResolution.INVALID

    def test_invalid_same_authority_different_content(self):
        """同权威 + 不同内容 → INVALID（互斥版本链）。"""
        from agent_runtime.features.memory.durable import ConflictResolution, _resolve_conflict

        existing = "decision v1"
        new = "decision v2 different"
        result = _resolve_conflict(existing, new, new_authority="auto")
        assert result == ConflictResolution.INVALID


class TestSourceToAuthority:
    def test_known_source_maps_correctly(self):
        from agent_runtime.features.memory.durable import source_to_authority

        assert source_to_authority("patcher") == "agent"
        assert source_to_authority("localizer") == "agent"
        assert source_to_authority("user") == "user"
        assert source_to_authority("stack_parse") == "agent"

    def test_unknown_source_defaults_auto(self):
        from agent_runtime.features.memory.durable import source_to_authority

        assert source_to_authority("unknown_tool") == "auto"
        assert source_to_authority("") == "auto"

    def test_partial_match_works(self):
        from agent_runtime.features.memory.durable import source_to_authority

        assert source_to_authority("tool:patcher") == "agent"


class TestUpsertEntryAuthority:
    """_upsert_entry 权威传播测试。"""

    def test_high_authority_overrides_low(self):
        from agent_runtime.features.memory.durable import DurableMemoryStore

        entries = ["old entry"]
        # 先以 auto 写入
        result = DurableMemoryStore._upsert_entry([], "old entry", authority="auto")
        # 同 subject、高权威覆盖
        result = DurableMemoryStore._upsert_entry(
            result, "old entry [authority:agent]", authority="agent"
        )
        assert len(result) == 1
        assert "agent" in result[0]

    def test_low_authority_appends_versioned(self):
        """低权威写入同 subject → 互斥版本链（追加 #v2）。"""
        from agent_runtime.features.memory.durable import DurableMemoryStore

        entries = ["important decision\nkind=decision confidence=0.80 source=agent [authority:agent]"]
        result = DurableMemoryStore._upsert_entry(
            entries, "important decision\nkind=observation confidence=0.50 source=auto",
            authority="auto",
        )
        # 低权威不可覆盖 → 追加版本
        assert len(result) == 2
        assert "v2" in result[1]

    def test_equivalent_overwrites_same(self):
        from agent_runtime.features.memory.durable import DurableMemoryStore

        entries = ["use ruff format"]
        result = DurableMemoryStore._upsert_entry(
            entries, "use ruff format", authority="auto"
        )
        assert len(result) == 1

    def test_new_entry_appended(self):
        from agent_runtime.features.memory.durable import DurableMemoryStore

        entries = ["existing entry"]
        result = DurableMemoryStore._upsert_entry(
            entries, "completely new entry", authority="auto"
        )
        assert len(result) == 2


# ---------------------------------------------------------------------------
# 互斥 key 版本链（V1.5-Bonus3）
# ---------------------------------------------------------------------------


class TestVersionChain:
    """同 subject 低权威写入追加版本链，保留历史。"""

    def test_invalid_creates_versioned_entry(self):
        from agent_runtime.features.memory.durable import DurableMemoryStore

        entries = ["important decision\nsource=agent [authority:agent]"]
        result = DurableMemoryStore._upsert_entry(
            entries, "important decision\nsource=auto",
            authority="auto",
        )
        assert len(result) == 2
        assert "v2" in result[1]

    def test_version_chain_length_increases(self):
        """连续 3 次低权威写入 → 版本链长度 3。"""
        from agent_runtime.features.memory.durable import DurableMemoryStore

        entries = ["locked decision [authority:agent]"]
        # 3 次低权威写入
        result = DurableMemoryStore._upsert_entry(
            entries, "locked decision\nattempt 1", authority="auto"
        )
        result = DurableMemoryStore._upsert_entry(
            result, "locked decision\nattempt 2", authority="auto"
        )
        assert len(result) == 3  # 原始 + v2 + v3
        assert "v2" in result[1]
        assert "v3" in result[2]

    def test_latest_entry_returns_newest(self):
        """_latest_entry 返回版本链末端条目。"""
        from agent_runtime.features.memory.durable import DurableMemoryStore

        entries = ["locked decision [authority:agent]"]
        result = DurableMemoryStore._upsert_entry(
            entries, "locked decision\nattempt 1", authority="auto"
        )
        result = DurableMemoryStore._upsert_entry(
            result, "locked decision\nattempt 2", authority="auto"
        )

        latest = DurableMemoryStore._latest_entry(result, "locked decision")
        assert latest is not None
        assert "attempt 2" in latest

    def test_entry_history_returns_all_versions(self):
        """_entry_history 返回全部版本（最旧在前）。"""
        from agent_runtime.features.memory.durable import DurableMemoryStore

        entries = ["locked decision [authority:agent]"]
        result = DurableMemoryStore._upsert_entry(
            entries, "locked decision\nattempt 1", authority="auto"
        )
        result = DurableMemoryStore._upsert_entry(
            result, "locked decision\nattempt 2", authority="auto"
        )

        history = DurableMemoryStore._entry_history(result, "locked decision")
        assert len(history) == 3
        assert "locked decision" in history[0]  # 原始
        assert "attempt 1" in history[1]
        assert "attempt 2" in history[2]  # 最新在最后

    def test_version_includes_timestamp(self):
        """版本链条目含 @timestamp。"""
        from agent_runtime.features.memory.durable import DurableMemoryStore

        entries = ["locked decision [authority:agent]"]
        result = DurableMemoryStore._upsert_entry(
            entries, "locked decision\nnew data", authority="auto"
        )
        assert "@" in result[1]  # 含时间戳标记

    def test_different_subjects_not_versioned(self):
        """不同 subject 的条目不参与版本链。"""
        from agent_runtime.features.memory.durable import DurableMemoryStore

        entries = ["entry alpha"]
        result = DurableMemoryStore._upsert_entry(
            entries, "entry beta", authority="auto"
        )
        assert len(result) == 2
        # beta 是新条目，无版本标记
        assert "v2" not in result[1]


# ---------------------------------------------------------------------------
# 置信度时间衰减（V1.5-Bonus3）
# ---------------------------------------------------------------------------


class TestConfidenceDecay:
    def test_apply_decay_formula(self):
        """apply_confidence_decay: confidence *= DECAY_RATE ** days。"""
        from agent_runtime.features.memory.durable import DECAY_RATE, apply_confidence_decay

        # 10 天前 → 0.95^10 ≈ 0.599
        old_time = 0  # epoch
        now = 10 * 86400  # 10 days later
        decayed, valid = apply_confidence_decay(1.0, old_time, now=now)
        expected = round(1.0 * (DECAY_RATE ** 10), 3)
        assert decayed == expected
        assert valid is True

    def test_recent_entry_not_decayed(self):
        """1 天内不衰减。"""
        from agent_runtime.features.memory.durable import apply_confidence_decay

        decayed, valid = apply_confidence_decay(0.5, 1000, now=1000 + 3600)  # 1 hour
        assert decayed == 0.5
        assert valid is True

    def test_old_entry_below_floor_expired(self):
        """置信度衰减后低于 CONFIDENCE_FLOOR → expired。"""
        from agent_runtime.features.memory.durable import apply_confidence_decay

        # 低初始置信度 + 很久以前 → 衰减到低于 0.1
        old_time = 0
        now = 200 * 86400  # 200 days
        decayed, valid = apply_confidence_decay(0.3, old_time, now=now)
        assert decayed < 0.1
        assert valid is False

    def test_high_confidence_survives_long_time(self):
        """高置信度条目久远但仍在阈值之上。"""
        from agent_runtime.features.memory.durable import apply_confidence_decay

        old_time = 0
        now = 30 * 86400  # 30 days
        decayed, valid = apply_confidence_decay(0.9, old_time, now=now)
        assert valid is True  # 0.9 * 0.95^30 ≈ 0.193 > 0.1

    def test_shared_formula_with_user_preference(self):
        """apply_confidence_decay 与 _apply_time_decay 共用公式。"""
        from agent_runtime.features.memory.durable import (
            UserPreference,
            _apply_time_decay,
            apply_confidence_decay,
        )

        pref = UserPreference(key="test", value="v", confidence=0.8, updated_at=0)
        result = _apply_time_decay([pref])
        # 置信度已衰减
        assert result[0].confidence <= 0.8


class TestEpisodicDecayInDream:
    """Dream._expire 对 episodic notes 应用时间衰减。"""

    def test_old_low_confidence_note_filtered(self):
        """旧 + 低置信度 → 被 Dream 过滤。"""
        from agent_runtime.features.memory.dream import MemoryDreamer
        from agent_runtime.features.memory.core import default_memory_state

        state = default_memory_state()
        old = 1000  # very old
        state["episodic_notes"] = [
            {"text": "old low conf", "note_index": 1, "created_at": old, "confidence": 0.3},
            {"text": "fresh note", "note_index": 2, "created_at": 1e12, "confidence": 0.8},
        ]
        dreamer = MemoryDreamer(state)
        # TTL=365 → 不过期，但衰减会过滤第一条
        removed = dreamer._expire(ttl_days=365)
        assert removed > 0
        notes = state["episodic_notes"]
        assert len(notes) == 1
        assert notes[0]["text"] == "fresh note"

    def test_no_confidence_field_not_decay_filtered(self):
        """无 confidence 字段的笔记不受衰减过滤（仅受 TTL 控制）。"""
        from agent_runtime.features.memory.dream import MemoryDreamer
        from agent_runtime.features.memory.core import default_memory_state

        state = default_memory_state()
        recent = time.time()
        state["episodic_notes"] = [
            {"text": "recent no conf", "note_index": 1, "created_at": recent},
        ]
        dreamer = MemoryDreamer(state)
        # TTL=-1 跳过 → 无 confidence 也不触发衰减 → 保留
        dreamer._expire(ttl_days=-1)
        assert len(state["episodic_notes"]) == 1


# ---------------------------------------------------------------------------
# 用户画像 schema（V1.5-Bonus3）
# ---------------------------------------------------------------------------


class TestUserProfileStore:
    @pytest.fixture
    def store(self, tmp_path):
        from agent_runtime.features.memory.durable import UserProfileStore

        return UserProfileStore(str(tmp_path))

    def test_set_and_get(self, store):
        store.set("editor", "vscode", source="user")
        pref = store.get("editor")
        assert pref is not None
        assert pref.value == "vscode"
        assert pref.source == "user"

    def test_list_all(self, store):
        store.set("test_framework", "pytest", confidence=0.9)
        store.set("formatter", "ruff")
        prefs = store.list_all()
        assert len(prefs) >= 2

    def test_update_existing(self, store):
        store.set("theme", "dark")
        store.set("theme", "light")  # 更新
        pref = store.get("theme")
        assert pref.value == "light"

    def test_remove_sets_empty(self, store):
        store.set("temp", "value")
        assert store.remove("temp") is True
        pref = store.get("temp")
        # 标记删除后存在但值为空
        assert pref is not None

    def test_get_nonexistent(self, store):
        assert store.get("nonexistent") is None

    def test_remove_nonexistent(self, store):
        assert store.remove("missing") is False

    def test_user_preference_to_dict(self):
        from agent_runtime.features.memory.durable import UserPreference

        pref = UserPreference(key="k", value="v", confidence=0.8, source="test")
        d = pref.to_dict()
        assert d["key"] == "k"
        assert d["value"] == "v"
        assert d["confidence"] == 0.8


class TestPromoteAuthority:
    """promote 权威传播集成测试。"""

    @pytest.fixture
    def store(self, tmp_path):
        (tmp_path / ".agent" / "memory" / "topics").mkdir(parents=True)
        return DurableMemoryStore(str(tmp_path))

    def test_agent_authority_allows_override(self, store):
        store.promote([("key-decisions", "auto decision")], authority="auto")
        # agent 权威覆盖同 subject 的 auto 条目
        store.promote([("key-decisions", "auto decision [authority:agent]")], authority="agent")
        entries = store._read_topic("key-decisions")
        assert len(entries) == 1
        assert "[authority:agent]" in entries[0]

    def test_auto_authority_cannot_override_agent(self, store):
        store.promote([("key-decisions", "agent decision\nsource=patcher")], authority="agent")
        # auto 权威不可覆盖 agent → 追加版本
        store.promote([("key-decisions", "agent decision\nsource=auto")], authority="auto")
        entries = store._read_topic("key-decisions")
        assert len(entries) == 2
        assert "source=patcher" in entries[0]
        assert "source=auto" in entries[1]
