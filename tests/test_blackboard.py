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
