"""RunStore trace 保留策略单测（V1.4-Bonus4b）。"""

from __future__ import annotations

import os
import tempfile
import time
from pathlib import Path

import pytest

from agent_runtime.run_store import RunStore, _DEFAULT_RUN_TTL_DAYS


# ---------------------------------------------------------------------------
# cleanup_older_than 基本功能
# ---------------------------------------------------------------------------


class TestCleanup:
    def test_no_runs_dir_returns_zero(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = RunStore(root=tmp)
            deleted = store.cleanup_older_than(days=30)
            assert deleted == 0

    def test_empty_runs_dir_returns_zero(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = RunStore(root=tmp)
            store.runs_dir.mkdir(parents=True, exist_ok=True)
            deleted = store.cleanup_older_than(days=30)
            assert deleted == 0

    def test_recent_run_not_deleted(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = RunStore(root=tmp)
            run_dir = store.runs_dir / "recent-run"
            run_dir.mkdir(parents=True)
            (run_dir / "task_state.json").write_text("{}")
            # 刚创建的 run 不应被删除
            deleted = store.cleanup_older_than(days=30)
            assert deleted == 0
            assert run_dir.is_dir()

    def test_old_run_is_deleted(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = RunStore(root=tmp)
            run_dir = store.runs_dir / "old-run"
            run_dir.mkdir(parents=True)
            (run_dir / "task_state.json").write_text("{}")
            # 模拟 60 天前的 mtime
            old_time = time.time() - 60 * 86400
            os.utime(run_dir, (old_time, old_time))
            deleted = store.cleanup_older_than(days=30)
            assert deleted == 1
            assert not run_dir.is_dir()

    def test_ttl_zero_disables_cleanup(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = RunStore(root=tmp)
            run_dir = store.runs_dir / "old-run"
            run_dir.mkdir(parents=True)
            old_time = time.time() - 60 * 86400
            os.utime(run_dir, (old_time, old_time))
            deleted = store.cleanup_older_than(days=0)
            assert deleted == 0
            assert run_dir.is_dir()

    def test_mixed_old_and_new(self):
        """旧 run 删除，新 run 保留。"""
        with tempfile.TemporaryDirectory() as tmp:
            store = RunStore(root=tmp)
            # 旧 run
            old = store.runs_dir / "old-run"
            old.mkdir(parents=True)
            os.utime(old, (time.time() - 60 * 86400,) * 2)
            # 新 run
            new = store.runs_dir / "new-run"
            new.mkdir(parents=True)
            deleted = store.cleanup_older_than(days=30)
            assert deleted == 1
            assert not old.is_dir()
            assert new.is_dir()

    def test_custom_days(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = RunStore(root=tmp)
            run_dir = store.runs_dir / "old-run"
            run_dir.mkdir(parents=True)
            os.utime(run_dir, (time.time() - 10 * 86400,) * 2)
            # 7 天 TTL → 10 天前的 run 被删
            deleted = store.cleanup_older_than(days=7)
            assert deleted == 1
            assert not run_dir.is_dir()

    def test_non_directory_entries_ignored(self):
        """非目录条目（如文件）不影响清理。"""
        with tempfile.TemporaryDirectory() as tmp:
            store = RunStore(root=tmp)
            store.runs_dir.mkdir(parents=True)
            (store.runs_dir / "README.txt").write_text("hello")
            deleted = store.cleanup_older_than(days=30)
            assert deleted == 0


# ---------------------------------------------------------------------------
# TTL 配置
# ---------------------------------------------------------------------------


class TestTTLConfig:
    def test_default_ttl_is_30(self):
        store = RunStore(root=".")
        assert store.ttl_days == _DEFAULT_RUN_TTL_DAYS

    def test_env_override(self):
        try:
            os.environ["FIXLOOP_RUN_TTL_DAYS"] = "7"
            store = RunStore(root=".")
            assert store.ttl_days == 7
        finally:
            del os.environ["FIXLOOP_RUN_TTL_DAYS"]

    def test_env_invalid_falls_back(self):
        try:
            os.environ["FIXLOOP_RUN_TTL_DAYS"] = "not_a_number"
            store = RunStore(root=".")
            assert store.ttl_days == _DEFAULT_RUN_TTL_DAYS
        finally:
            del os.environ["FIXLOOP_RUN_TTL_DAYS"]


# ---------------------------------------------------------------------------
# 自动清理 — start_run 触发
# ---------------------------------------------------------------------------


class TestAutoCleanup:
    def test_start_run_triggers_cleanup(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = RunStore(root=tmp)
            old_run = store.runs_dir / "old-run"
            old_run.mkdir(parents=True)
            os.utime(old_run, (time.time() - 60 * 86400,) * 2)
            # start_run 应自动清理
            run_dir = store.start_run_by_id("new-run")
            assert run_dir.is_dir()
            assert not old_run.is_dir()

    def test_start_run_does_not_delete_recent(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = RunStore(root=tmp)
            recent = store.runs_dir / "recent-run"
            recent.mkdir(parents=True)
            store.start_run_by_id("new-run")
            assert recent.is_dir()
