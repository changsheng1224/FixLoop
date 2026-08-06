"""RepairState 数据模型单测：JSON 往返序列化。"""

from src.state import (
    CandidatePatch,
    RepairPlan,
    RepairState,
    RepairSubTask,
    RetrievedContext,
    SuspectLocation,
    VerificationResult,
)


class TestSuspectLocation:
    def test_json_roundtrip(self):
        s = SuspectLocation(
            file_path="calc.py",
            start_line=42,
            end_line=44,
            function_name="add",
            reason="堆栈指向",
            confidence=0.95,
        )
        restored = SuspectLocation.from_dict(s.to_dict())
        assert restored.file_path == "calc.py"
        assert restored.confidence == 0.95


class TestRepairPlan:
    def test_json_roundtrip(self):
        p = RepairPlan(
            language="python",
            language_source="extension:.py",
            issue_type="type_error",
            suspect_files=["calc.py"],
            reasoning="TypeError at line 42",
            prompt_variants={"patcher": "type_error"},
        )
        restored = RepairPlan.from_dict(p.to_dict())
        assert restored.issue_type == "type_error"
        assert restored.language_source == "extension:.py"
        assert restored.prompt_variants == {"patcher": "type_error"}


class TestRetrievedContext:
    def test_json_roundtrip(self):
        ctx = RetrievedContext(
            similar_snippets=[{"file": "a.py", "snippet": "code"}],
            caller_locations=["main.py:10"],
            related_tests=["test_calc.py::test_add"],
        )
        restored = RetrievedContext.from_dict(ctx.to_dict())
        assert len(restored.related_tests) == 1


class TestCandidatePatch:
    def test_json_roundtrip(self):
        p = CandidatePatch(file_path="calc.py", diff="-old\n+new", explanation="修复类型转换")
        restored = CandidatePatch.from_dict(p.to_dict())
        assert restored.file_path == "calc.py"


class TestVerificationResult:
    def test_json_roundtrip(self):
        v = VerificationResult(
            all_passed=True,
            total_tests=12,
            passed=12,
        )
        restored = VerificationResult.from_dict(v.to_dict())
        assert restored.all_passed


class TestRepairState:
    def test_full_roundtrip(self):
        state = RepairState(issue_input="TypeError at calc.py:42")
        state.suspect_locations.append(
            SuspectLocation(
                file_path="calc.py",
                start_line=42,
                end_line=44,
                function_name="add",
                reason="堆栈",
                confidence=0.95,
            )
        )
        state.candidate_patches.append(CandidatePatch(file_path="calc.py", diff="-old\n+new"))
        state.status = "fixed"

        data = state.to_dict()
        restored = RepairState.from_dict(data)

        assert restored.issue_input == "TypeError at calc.py:42"
        assert len(restored.suspect_locations) == 1
        assert restored.suspect_locations[0].file_path == "calc.py"
        assert len(restored.candidate_patches) == 1
        assert restored.status == "fixed"

    def test_empty_state_roundtrip(self):
        state = RepairState(issue_input="test")
        restored = RepairState.from_dict(state.to_dict())
        assert restored.suspect_locations == []
        assert restored.candidate_patches == []
        assert restored.status == "pending"


class TestRepairPhase:
    def test_default_phase_is_localize(self):
        state = RepairState(issue_input="test")
        assert state.phase == "localize"
        assert state.status == "pending"

    def test_phase_independent_of_status(self):
        state = RepairState(issue_input="test", status="fixed")
        assert state.phase == "localize"
        assert state.status == "fixed"

    def test_phase_roundtrip(self):
        state = RepairState(issue_input="test", phase="retrieve")
        data = state.to_dict()
        restored = RepairState.from_dict(data)
        assert restored.phase == "retrieve"

    def test_phase_transitions(self):
        from src.state import REPAIR_PHASES

        assert "localize" in REPAIR_PHASES
        assert "retrieve" in REPAIR_PHASES
        assert "patch" in REPAIR_PHASES
        assert "verify" in REPAIR_PHASES
        assert "done" in REPAIR_PHASES
        assert "failed" in REPAIR_PHASES


class TestRepairStatePersistence:
    def test_full_state_to_dict_includes_phase_and_timings(self):
        state = RepairState(
            issue_input="test",
            phase="verify",
            status="fixed",
            retry_count=1,
            node_timings={"phases": {"localize_ms": 100, "patch_ms": 200}},
        )
        data = state.to_dict()
        assert data["phase"] == "verify"
        assert data["status"] == "fixed"
        assert data["retry_count"] == 1
        assert "node_timings" in data

    def test_persist_and_restore_roundtrip(self, tmp_path):
        import json

        state = RepairState(
            issue_input="TypeError at calc.py:1",
            phase="done",
            status="fixed",
            node_timings={"phases": {"localize_ms": 50, "verify_ms": 300}},
        )
        path = tmp_path / "repair_state.json"
        path.write_text(json.dumps(state.to_dict(), ensure_ascii=False, indent=2))
        data = json.loads(path.read_text())
        restored = RepairState.from_dict(data)
        assert restored.issue_input == state.issue_input
        assert restored.status == "fixed"
        assert restored.phase == "done"


class TestBlackboardSnapshot:
    def test_snapshot_in_state_to_dict(self):
        state = RepairState(
            issue_input="test",
            blackboard_snapshot={"entries": {"suspect:calc.py": "..."}, "conflicts": []},
        )
        data = state.to_dict()
        assert "blackboard_snapshot" in data
        assert data["blackboard_snapshot"]["conflicts"] == []


# ---------------------------------------------------------------------------
# RepairSubTask（V1.4-Bonus15b）
# ---------------------------------------------------------------------------


class TestRepairSubTask:
    def test_create_minimal(self):
        st = RepairSubTask(id="fix_import", goal="add missing import")
        assert st.id == "fix_import"
        assert st.suspect_files == []
        assert st.depends_on == []

    def test_roundtrip_to_from_dict(self):
        st = RepairSubTask(id="s1", goal="fix", suspect_files=["a.py"], depends_on=["s0"])
        d = st.to_dict()
        st2 = RepairSubTask.from_dict(d)
        assert st2.id == "s1"
        assert st2.suspect_files == ["a.py"]
        assert st2.depends_on == ["s0"]


class TestRepairPlanSubtasks:
    def test_default_empty(self):
        plan = RepairPlan(issue_type="type_error")
        assert plan.subtasks == []

    def test_with_subtasks(self):
        plan = RepairPlan(
            issue_type="composite",
            subtasks=[
                RepairSubTask(id="1", goal="fix import", suspect_files=["a.py"]),
                RepairSubTask(id="2", goal="fix type", suspect_files=["b.py"], depends_on=["1"]),
            ],
        )
        assert len(plan.subtasks) == 2
        assert plan.subtasks[1].depends_on == ["1"]

    def test_roundtrip_with_subtasks(self):
        plan = RepairPlan(issue_type="composite", subtasks=[RepairSubTask(id="1", goal="fix")])
        d = plan.to_dict()
        assert len(d["subtasks"]) == 1
        plan2 = RepairPlan.from_dict(d)
        assert len(plan2.subtasks) == 1
        assert plan2.subtasks[0].id == "1"
