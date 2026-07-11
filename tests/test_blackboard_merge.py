"""Blackboard merge：write / merge / dedupe 单测。"""

from src.blackboard import Blackboard
from src.repair.blackboard_merge import (
    merge_blackboard_for_patch,
    read_suspects_from_blackboard,
    resolve_blackboard_conflicts,
    suspect_key,
    write_feedback_to_blackboard,
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


class TestMergeBlackboardForPatch:
    def test_merge_reads_blackboard_without_prior_state(self):
        bb = Blackboard()
        suspects = [
            SuspectLocation(file_path="calc.py", start_line=42, end_line=44, confidence=0.95)
        ]
        context = RetrievedContext(related_tests=["test_calc.py"])
        write_localize_phase_to_blackboard(bb, suspects, context)
        state = RepairState(issue_input="TypeError")
        assert state.suspect_locations == []
        meta = merge_blackboard_for_patch(state, bb)
        assert len(state.suspect_locations) == 1
        assert state.suspect_locations[0].file_path == "calc.py"
        assert state.retrieved_context.related_tests == ["test_calc.py"]
        assert meta["suspect_count"] == 1
        assert len(read_suspects_from_blackboard(bb)) == 1

    def test_dedupe_keeps_higher_confidence(self):
        bb = Blackboard()
        low = SuspectLocation(file_path="x.py", start_line=10, end_line=10, confidence=0.3)
        high = SuspectLocation(file_path="x.py", start_line=10, end_line=12, confidence=0.9)
        bb.write("suspect:x.py:10", low.to_dict(), source_agent="localizer")
        bb.write("suspect:x.py:10-alt", high.to_dict(), source_agent="localizer")
        state = RepairState(issue_input="err")
        merge_blackboard_for_patch(state, bb)
        assert len(state.suspect_locations) == 1
        assert state.suspect_locations[0].confidence == 0.9

    def test_resolve_conflict_prefer_localizer(self):
        bb = Blackboard()
        loc = {"file_path": "k.py", "start_line": 1, "end_line": 1, "confidence": 0.8}
        ret = {"file_path": "k.py", "start_line": 1, "end_line": 2, "confidence": 0.9}
        bb.write("suspect:k.py:1", loc, source_agent="localizer")
        bb.write("suspect:k.py:1", ret, source_agent="retriever")
        assert len(bb.conflicts) == 1
        resolved = resolve_blackboard_conflicts(bb, strategy="prefer_localizer")
        assert len(resolved) == 1
        assert resolved[0]["winner_source"] == "localizer"
        assert bb.read("suspect:k.py:1")["end_line"] == 1
        assert bb.conflicts == []

    def test_scratch_feedback_applied_when_state_empty(self):
        bb = Blackboard()
        write_localize_phase_to_blackboard(
            bb,
            [SuspectLocation(file_path="a.py", start_line=1, end_line=1)],
            None,
        )
        write_feedback_to_blackboard(bb, "pytest failed: assert 1 == 2")
        state = RepairState(issue_input="err", feedback="")
        meta = merge_blackboard_for_patch(state, bb)
        assert meta["scratch_feedback_applied"] is True
        assert "assert 1 == 2" in state.feedback

    def test_merge_materializes_suspects(self):
        bb = Blackboard()
        write_localize_phase_to_blackboard(
            bb,
            [SuspectLocation(file_path="z.py", start_line=3, end_line=3)],
            None,
        )
        state = RepairState(issue_input="err")
        meta = merge_blackboard_for_patch(state, bb)
        assert meta["suspect_count"] == 1
        assert state.suspect_locations[0].file_path == "z.py"

    def test_conflict_recorded_before_resolve(self):
        bb = Blackboard()
        bb.write("suspect:k.py:1", {"file_path": "k.py", "start_line": 1, "end_line": 1}, "localizer")
        bb.write("suspect:k.py:1", {"file_path": "k.py", "start_line": 1, "end_line": 2}, "retriever")
        state = RepairState(issue_input="err")
        meta = merge_blackboard_for_patch(state, bb)
        assert meta["conflicts_resolved"]
        assert bb.conflicts == []
