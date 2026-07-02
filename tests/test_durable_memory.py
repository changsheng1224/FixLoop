"""Durable Memory 单测：读写 Markdown 文件、promote、过滤。"""

import tempfile

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
        store.promote([
            ("user-preferences", "Preference: default timeout is 30s"),
            ("key-decisions", "Decision: use urllib instead of requests"),
        ])
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
