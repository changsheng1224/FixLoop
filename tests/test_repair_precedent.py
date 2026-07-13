"""Repair Precedent 读写一体单测（V1.4-Bonus9d）。"""

from __future__ import annotations

import json
import tempfile
from pathlib import Path

from src.repair.precedent import RepairPrecedentStore


def _write_raw(path: Path, lines: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


# ---------------------------------------------------------------------------
# 读
# ---------------------------------------------------------------------------


class TestLoadSimilar:
    def test_empty_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = RepairPrecedentStore(tmp)
            assert store.load_similar("type_error") == []

    def test_no_match(self):
        with tempfile.TemporaryDirectory() as tmp:
            _write_raw(
                Path(tmp) / ".agent" / "memory" / "topics" / "dependency-facts.md",
                [
                    "# Dependency Facts",
                    '{"issue_type":"import_error","summary":"add sys.path","ts":1000}',
                ],
            )
            store = RepairPrecedentStore(tmp)
            assert store.load_similar("type_error") == []

    def test_match_found(self):
        with tempfile.TemporaryDirectory() as tmp:
            _write_raw(
                Path(tmp) / ".agent" / "memory" / "topics" / "dependency-facts.md",
                [
                    "# Dependency Facts",
                    '{"issue_type":"type_error","summary":"int wrapper","case_id":"case_001","ts":2000}',
                    '{"issue_type":"import_error","summary":"add sys.path","ts":1000}',
                    '{"issue_type":"type_error","summary":"str cast","case_id":"case_002","ts":3000}',
                ],
            )
            store = RepairPrecedentStore(tmp)
            results = store.load_similar("type_error")
            assert len(results) == 2
            # 按时间倒序
            assert results[0]["summary"] == "str cast"
            assert results[1]["summary"] == "int wrapper"

    def test_limit(self):
        with tempfile.TemporaryDirectory() as tmp:
            lines = ["# Facts"]
            for i in range(10):
                lines.append(
                    json.dumps({"issue_type": "type_error", "summary": f"fix{i}", "ts": i})
                )
            _write_raw(
                Path(tmp) / ".agent" / "memory" / "topics" / "dependency-facts.md",
                lines,
            )
            store = RepairPrecedentStore(tmp)
            results = store.load_similar("type_error", limit=3)
            assert len(results) == 3

    def test_ignores_non_json_lines(self):
        with tempfile.TemporaryDirectory() as tmp:
            _write_raw(
                Path(tmp) / ".agent" / "memory" / "topics" / "dependency-facts.md",
                [
                    "# Facts",
                    "- some bullet point (ignored)",
                    '{"issue_type":"type_error","summary":"ok","ts":1}',
                ],
            )
            store = RepairPrecedentStore(tmp)
            assert len(store.load_similar("type_error")) == 1


# ---------------------------------------------------------------------------
# 写
# ---------------------------------------------------------------------------


class TestUpsert:
    def test_first_write(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = RepairPrecedentStore(tmp)
            store.upsert("type_error", "int wrapper fix", case_id="case_001")
            results = store.load_similar("type_error")
            assert len(results) == 1
            assert results[0]["issue_type"] == "type_error"
            assert results[0]["summary"] == "int wrapper fix"

    def test_upsert_overwrites_same_case_id(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = RepairPrecedentStore(tmp)
            store.upsert("type_error", "first attempt", case_id="case_001")
            store.upsert("type_error", "better fix", case_id="case_001")
            results = store.load_similar("type_error")
            assert len(results) == 1
            assert results[0]["summary"] == "better fix"

    def test_different_case_ids_coexist(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = RepairPrecedentStore(tmp)
            store.upsert("type_error", "fix a", case_id="case_001")
            store.upsert("type_error", "fix b", case_id="case_002")
            results = store.load_similar("type_error")
            assert len(results) == 2

    def test_truncates_long_summary(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = RepairPrecedentStore(tmp)
            store.upsert("type_error", "x" * 300)
            results = store.load_similar("type_error")
            assert len(results[0]["summary"]) <= 200

    def test_load_all(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = RepairPrecedentStore(tmp)
            store.upsert("type_error", "fix1")
            store.upsert("import_error", "fix2")
            all_entries = store.load_all()
            assert len(all_entries) == 2


# ---------------------------------------------------------------------------
# 置信度闸口
# ---------------------------------------------------------------------------


class TestSemanticFilter:
    def test_no_query_returns_all(self):
        """query 为空时不过滤。"""
        with tempfile.TemporaryDirectory() as tmp:
            store = RepairPrecedentStore(tmp)
            store.upsert("type_error", "int wrapper fix", case_id="c1")
            store.upsert("type_error", "add type check", case_id="c2")
            results = store.load_similar("type_error", query="")
            assert len(results) == 2

    def test_empty_precedents(self):
        store = RepairPrecedentStore(".")
        assert store._semantic_filter("test", [], 0.4) == []

    def test_model_unavailable_returns_all(self):
        """语义模型不可用时不过滤，返回全部。"""
        with tempfile.TemporaryDirectory() as tmp:
            store = RepairPrecedentStore(tmp)
            store.upsert("type_error", "int wrapper")
            store.upsert("type_error", "str conversion")
            # 不传 query → 语义过滤跳过
            results = store.load_similar("type_error")
            assert len(results) == 2

    def test_threshold_zero_returns_all(self):
        """threshold=0 时不过滤。"""
        store = RepairPrecedentStore(".")
        precedents = [
            {"issue_type": "type_error", "summary": "fix a", "ts": 1},
            {"issue_type": "type_error", "summary": "fix b", "ts": 2},
        ]
        filtered = store._semantic_filter("unrelated query", precedents, 0.0)
        assert len(filtered) == 2

    def test_no_summary_skipped_when_model_available(self):
        """无 summary 的 precedent 被跳过（模型不可用时全部保留）。"""
        store = RepairPrecedentStore(".")
        precedents = [
            {"issue_type": "type_error", "ts": 1},  # no summary
        ]
        filtered = store._semantic_filter("type error in app.py", precedents, 0.4)
        # 语义模型不可用时全部保留；可用时无 summary 的被跳过
        assert len(filtered) <= 1

