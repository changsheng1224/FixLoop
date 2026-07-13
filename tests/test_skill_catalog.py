"""SkillCatalog content_hash + 原子 swap 单测（V1.4-Bonus13a）。"""

from __future__ import annotations

import json
import tempfile
from pathlib import Path

import yaml

from src.skills.catalog import SkillCatalog, _compute_directory_hash


# ---------------------------------------------------------------------------
# content_hash
# ---------------------------------------------------------------------------


class TestContentHash:
    def test_hash_changes_when_file_changes(self):
        with tempfile.TemporaryDirectory() as tmp:
            d = Path(tmp)
            (d / "test.yaml").write_text(
                yaml.dump({"name": "test_a", "language": "python",
                           "trigger_pattern": "TypeError", "priority": 10,
                           "guidance": ["check types"]})
            )
            h1 = _compute_directory_hash(d)
            (d / "test.yaml").write_text(
                yaml.dump({"name": "test_b", "language": "python",
                           "trigger_pattern": "ValueError", "priority": 20,
                           "guidance": ["check values"]})
            )
            h2 = _compute_directory_hash(d)
            assert h1 != h2

    def test_hash_stable_for_unchanged_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            d = Path(tmp)
            (d / "test.yaml").write_text(
                yaml.dump({"name": "test", "language": "python",
                           "trigger_pattern": "Error", "priority": 5,
                           "guidance": ["fix"]})
            )
            h1 = _compute_directory_hash(d)
            h2 = _compute_directory_hash(d)
            assert h1 == h2

    def test_empty_directory_returns_consistent_hash(self):
        with tempfile.TemporaryDirectory() as tmp:
            h = _compute_directory_hash(Path(tmp))
            assert len(h) == 64

    def test_catalog_has_content_hash(self):
        """load_from_directory 产出 ContentHash。"""
        with tempfile.TemporaryDirectory() as tmp:
            d = Path(tmp)
            (d / "test.yaml").write_text(
                yaml.dump({"name": "test", "language": "python",
                           "trigger_pattern": "Error", "priority": 5,
                           "guidance": ["fix"]})
            )
            cat = SkillCatalog.load_from_directory(d)
            assert cat.content_hash
            assert len(cat.content_hash) == 64
            assert cat.skill_count == 1


# ---------------------------------------------------------------------------
# 原子 swap rebuild_index
# ---------------------------------------------------------------------------


class TestRebuildIndex:
    def test_first_build_creates_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            d, cache = _setup(tmp)
            cat = SkillCatalog.load_from_directory(d)
            result = cat.rebuild_index(cache)
            assert result is True
            assert cache.is_file()

    def test_second_build_no_change_returns_false(self):
        with tempfile.TemporaryDirectory() as tmp:
            d, cache = _setup(tmp)
            cat = SkillCatalog.load_from_directory(d)
            assert cat.rebuild_index(cache) is True
            # 第二次：内容未变 → False
            cat2 = SkillCatalog.load_from_directory(d)
            assert cat2.rebuild_index(cache) is False

    def test_content_change_triggers_rebuild(self):
        with tempfile.TemporaryDirectory() as tmp:
            d, cache = _setup(tmp)
            cat = SkillCatalog.load_from_directory(d)
            cat.rebuild_index(cache)

            # 修改 skill
            (d / "test.yaml").write_text(
                yaml.dump({"name": "new_skill", "language": "python",
                           "trigger_pattern": "NewError", "priority": 99,
                           "guidance": ["fix new"]})
            )
            cat2 = SkillCatalog.load_from_directory(d)
            assert cat2.rebuild_index(cache) is True

    def test_index_contains_content_hash(self):
        with tempfile.TemporaryDirectory() as tmp:
            d, cache = _setup(tmp)
            cat = SkillCatalog.load_from_directory(d)
            cat.rebuild_index(cache)
            data = json.loads(cache.read_text(encoding="utf-8"))
            assert data["content_hash"] == cat.content_hash
            assert data["skill_count"] == cat.skill_count

    def test_atomic_write_uses_tmp_then_replace(self):
        with tempfile.TemporaryDirectory() as tmp:
            d, cache = _setup(tmp)
            cat = SkillCatalog.load_from_directory(d)
            cat.rebuild_index(cache)
            # tmp 文件已被 rename 到 cache，不应残留
            tmp_file = cache.with_suffix(".tmp")
            assert not tmp_file.is_file()


def _setup(tmp: str) -> tuple[Path, Path]:
    d = Path(tmp)
    (d / "test.yaml").write_text(
        yaml.dump({"name": "test_skill", "language": "python",
                   "trigger_pattern": "Error", "priority": 10,
                   "guidance": ["check types"]})
    )
    cache = d / ".agent" / ".skill_index.json"
    return d, cache
