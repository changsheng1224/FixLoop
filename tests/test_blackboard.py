"""Blackboard 单测：读写、冲突、TTL。"""

import time

from src.blackboard import Blackboard


class TestBlackboardReadWrite:
    def test_write_and_read(self):
        bb = Blackboard()
        bb.write("key1", "val1", source_agent="localizer")
        assert bb.read("key1") == "val1"

    def test_same_source_overwrites(self):
        bb = Blackboard()
        bb.write("k", "v1", source_agent="localizer")
        bb.write("k", "v2", source_agent="localizer")
        assert bb.read("k") == "v2"

    def test_different_source_conflict(self):
        bb = Blackboard()
        ok1 = bb.write("k", "v1", source_agent="localizer")
        ok2 = bb.write("k", "v2", source_agent="retriever")
        assert ok1 is True
        assert ok2 is False  # 冲突，没有覆盖
        assert bb.read("k") == "v1"  # 保留第一个
        assert len(bb.conflicts) == 1

    def test_read_related_prefix(self):
        bb = Blackboard()
        bb.write("suspect:calc.py", "loc", source_agent="localizer")
        bb.write("suspect:main.py", "loc2", source_agent="localizer")
        bb.write("other:key", "val", source_agent="retriever")
        results = bb.read_related("suspect:")
        assert len(results) == 2

    def test_ttl_expiry(self):
        bb = Blackboard()
        bb.write("k", "v", source_agent="localizer", ttl=0.05)
        assert bb.read("k") == "v"
        time.sleep(0.1)
        assert bb.read("k") is None

    def test_snapshot(self):
        bb = Blackboard()
        bb.write("k", "v", source_agent="localizer")
        snap = bb.snapshot()
        assert snap["entries"]["k"] == "v"
        assert snap["conflicts"] == []

    def test_apply_conflict_winner(self):
        bb = Blackboard()
        bb.write("k", "v1", source_agent="localizer")
        bb.write("k", "v2", source_agent="retriever")
        assert len(bb.conflicts) == 1
        bb.apply_conflict_winner("k", "v1", "localizer")
        assert bb.read("k") == "v1"
        assert bb.conflicts == []

class TestOrchestratorConflictAPI:
    def test_resolve_conflict_prefer_localizer(self):
        from src.blackboard import Blackboard
        from src.repair.blackboard_merge import resolve_blackboard_conflicts

        bb = Blackboard()
        bb.write("suspect:calc.py:42", {"confidence": 0.8}, source_agent="retriever")
        bb.write("suspect:calc.py:42", {"confidence": 0.95}, source_agent="localizer")
        # 冲突已记录
        assert len(bb.conflicts) == 1
        resolved = resolve_blackboard_conflicts(bb, strategy="prefer_localizer")
        assert len(resolved) == 1
        assert resolved[0]["strategy"] == "prefer_localizer"

    def test_no_conflict_returns_empty(self):
        from src.blackboard import Blackboard
        from src.repair.blackboard_merge import resolve_blackboard_conflicts

        bb = Blackboard()
        bb.write("suspect:calc.py:42", {"confidence": 0.8}, source_agent="localizer")
        assert resolve_blackboard_conflicts(bb) == []

class TestSuspectDedupe:
    def test_dedupes_same_file_line(self):
        from src.repair.blackboard_merge import dedupe_suspects
        from src.state import SuspectLocation

        s1 = SuspectLocation(file_path="a.py", start_line=1, end_line=1, confidence=0.5)
        s2 = SuspectLocation(file_path="a.py", start_line=1, end_line=1, confidence=0.9)
        s3 = SuspectLocation(file_path="b.py", start_line=2, end_line=2, confidence=0.7)
        result = dedupe_suspects([s1, s2, s3])
        assert len(result) == 2
        assert result[0].confidence == 0.9  # takes higher confidence
