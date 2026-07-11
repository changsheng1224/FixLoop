"""Blackboard merge：write / merge / dedupe 单测。"""

from src.blackboard import Blackboard
from src.repair.blackboard_merge import (
    merge_blackboard_to_repair_state,
    suspect_key,
    write_localize_phase_to_blackboard,
)
from src.state import RepairState, RetrievedContext, SuspectLocation


class TestSuspectKey:
    def test_suspect_key_format(self):
        s = SuspectLocation(file_path="calc.py", start_line=42, end_line=44)
        assert suspect_key(s) == "suspect:calc.py:42"


class TestWriteLocalizePhase:
    def test_writes_suspects_and_context(self):
        bb = Blackboard()
        suspects = [
            SuspectLocation(
                file_path="calc.py",
                start_line=42,
                end_line=44,
                confidence=0.9,
            )
        ]
        context = RetrievedContext(related_tests=["test_calc.py::test_add"])
        stats = write_localize_phase_to_blackboard(bb, suspects, context)
        assert stats["suspects_written"] == 1
        assert stats["context_keys_written"] == 1
        assert bb.read("suspect:calc.py:42")["file_path"] == "calc.py"
        assert bb.read("context:related_tests") == ["test_calc.py::test_add"]

    def test_same_source_overwrites_suspect(self):
        bb = Blackboard()
        s1 = SuspectLocation(file_path="a.py", start_line=1, end_line=1, confidence=0.5)
        s2 = SuspectLocation(file_path="a.py", start_line=1, end_line=2, confidence=0.9)
        write_localize_phase_to_blackboard(bb, [s1], None)
        write_localize_phase_to_blackboard(bb, [s2], None)
        assert bb.read("suspect:a.py:1")["confidence"] == 0.9


class TestMergeBlackboardToRepairState:
    def test_merge_materializes_repair_state(self):
        bb = Blackboard()
        suspects = [
            SuspectLocation(file_path="calc.py", start_line=42, end_line=44, confidence=0.95)
        ]
        context = RetrievedContext(related_tests=["test_calc.py"])
        write_localize_phase_to_blackboard(bb, suspects, context)
        state = RepairState(issue_input="TypeError")
        meta = merge_blackboard_to_repair_state(state, bb)
        assert len(state.suspect_locations) == 1
        assert state.suspect_locations[0].file_path == "calc.py"
        assert state.retrieved_context.related_tests == ["test_calc.py"]
        assert meta["suspect_count"] == 1
        assert "entries" in state.blackboard_snapshot

    def test_dedupe_keeps_higher_confidence(self):
        bb = Blackboard()
        low = SuspectLocation(file_path="x.py", start_line=10, end_line=10, confidence=0.3)
        high = SuspectLocation(file_path="x.py", start_line=10, end_line=12, confidence=0.9)
        bb.write("suspect:x.py:10", low.to_dict(), source_agent="localizer")
        bb.write("suspect:x.py:10-alt", high.to_dict(), source_agent="localizer")
        state = RepairState(issue_input="err")
        merge_blackboard_to_repair_state(state, bb)
        assert len(state.suspect_locations) == 1
        assert state.suspect_locations[0].confidence == 0.9

    def test_conflict_recorded_in_snapshot(self):
        bb = Blackboard()
        bb.write("suspect:k.py:1", {"file_path": "k.py", "start_line": 1, "end_line": 1}, "localizer")
        bb.write("suspect:k.py:1", {"file_path": "k.py", "start_line": 1, "end_line": 2}, "retriever")
        state = RepairState(issue_input="err")
        meta = merge_blackboard_to_repair_state(state, bb)
        assert len(meta["conflicts"]) == 1
        assert len(state.blackboard_snapshot["conflicts"]) == 1
