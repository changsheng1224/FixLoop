"""SessionStore .bak 单测：保存→bak + 损坏主文件→回退 bak。"""

import json
import tempfile
from pathlib import Path

import pytest


class TestSessionStoreBak:
    @pytest.fixture
    def store(self, tmp_path):
        from agent_runtime.session_store import SessionStore

        (tmp_path / ".agent" / "sessions").mkdir(parents=True)
        return SessionStore(str(tmp_path))

    def test_save_creates_bak(self, store):
        """save() 后 .json.bak 文件存在。"""
        store.save({"id": "test-session", "data": "hello"})
        bak_path = store.sessions_dir / "test-session.json.bak"
        assert bak_path.is_file()

    def test_bak_content_matches_main(self, store):
        """.bak 内容与主文件一致。"""
        data = {"id": "s1", "key": "value", "nested": {"a": 1}}
        store.save(data)
        main = json.loads((store.sessions_dir / "s1.json").read_text(encoding="utf-8"))
        bak = json.loads((store.sessions_dir / "s1.json.bak").read_text(encoding="utf-8"))
        assert main == bak

    def test_load_falls_back_to_bak_when_main_corrupted(self, store):
        """主文件损坏时 load() 回退到 .bak。"""
        store.save({"id": "s2", "data": "original"})
        # 损坏主文件
        (store.sessions_dir / "s2.json").write_text("not valid json{{{", encoding="utf-8")
        # load 应回退到 bak
        loaded = store.load("s2")
        assert loaded is not None
        assert loaded["data"] == "original"

    def test_load_returns_none_when_both_corrupted(self, store):
        """主文件和 bak 都损坏时返回 None。"""
        store.save({"id": "s3", "data": "doomed"})
        (store.sessions_dir / "s3.json").write_text("bad", encoding="utf-8")
        (store.sessions_dir / "s3.json.bak").write_text("also bad", encoding="utf-8")
        loaded = store.load("s3")
        assert loaded is None

    def test_load_returns_none_when_no_files(self, store):
        """文件不存在时返回 None。"""
        assert store.load("nonexistent") is None
