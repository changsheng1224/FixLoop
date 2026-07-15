"""L2 --resume-repair 续跑单测：写盘→load→跳过 parse/localize。"""

from pathlib import Path

import pytest

from src.repair.checkpoint_load import (
    load_repair_checkpoint,
    save_repair_checkpoint,
)
from src.state import RepairPlan, RepairState, SuspectLocation


class TestSaveLoadCheckpoint:
    def test_save_and_load_roundtrip(self, tmp_path):
        """保存 → 加载字段一致。"""
        state = RepairState(issue_input="TypeError at calc.py:42")
        state.repair_run_id = "test-run-001"
        state.retry_count = 2
        state.phase = "patch"
        state.feedback = "test feedback"
        state.suspect_locations = [
            SuspectLocation(file_path="calc.py", start_line=42, end_line=44, confidence=0.9)
        ]
        state.blackboard_snapshot = {"entries": {"key": "val"}}

        repo = str(tmp_path)
        # 手动创建 runs dir
        (Path(repo) / ".agent" / "runs" / "test-run-001").mkdir(parents=True)
        save_repair_checkpoint(state, repo)

        loaded = load_repair_checkpoint(repo, "test-run-001")
        assert loaded is not None
        assert loaded["retry_count"] == 2
        assert loaded["phase"] == "patch"
        assert loaded["feedback"] == "test feedback"
        assert len(loaded["suspect_locations"]) == 1
        assert loaded["blackboard_snapshot"] == {"entries": {"key": "val"}}

    def test_load_nonexistent_returns_none(self, tmp_path):
        loaded = load_repair_checkpoint(str(tmp_path), "nonexistent")
        assert loaded is None

    def test_load_corrupted_returns_none(self, tmp_path):
        repo = str(tmp_path)
        path = Path(repo) / ".agent" / "runs" / "bad-run" / "repair_checkpoint.json"
        path.parent.mkdir(parents=True)
        path.write_text("not json", encoding="utf-8")
        loaded = load_repair_checkpoint(repo, "bad-run")
        assert loaded is None


class TestSaveCheckpointValidatesType:
    def test_non_repair_state_raises(self, tmp_path):
        with pytest.raises(TypeError):
            save_repair_checkpoint({"not": "a state"}, str(tmp_path))

    def test_missing_run_id_raises(self, tmp_path):
        state = RepairState(issue_input="test")
        with pytest.raises(ValueError, match="repair_run_id"):
            save_repair_checkpoint(state, str(tmp_path))


class TestResumeSkipsParse:
    def test_orchestrator_repair_passes_resume_run_id_to_state(self, tmp_path):
        """Orchestrator.repair 应将 resume_run_id 写入 RepairState。"""
        from src.orchestrator import Orchestrator

        class CaptureResumeOrchestrator(Orchestrator):
            def _snapshot_repo(self):
                return {}

            def _restore_repo_snapshot(self, snapshot):
                return None

            def _repair_impl(self, state, initial_snapshot=None):
                return state

        orch = CaptureResumeOrchestrator(None, None, None)
        orch._repo_root = str(tmp_path)

        result = orch.repair(
            "test issue",
            repair_timeout_s=0,
            resume_run_id="resume-001",
        )

        assert result.repair_run_id == "resume-001"

    def test_resume_repair_skips_parse(self, tmp_path):
        """checkpoint 字段恢复保留 retry/phase/suspects 等关键状态。"""
        from src.repair.pipeline import RepairPipelineMixin
        from src.state import RepairState, SuspectLocation

        repo = str(tmp_path)
        # 保存 checkpoint
        state = RepairState(issue_input="test issue")
        state.repair_run_id = "resume-001"
        state.retry_count = 1
        state.phase = "patch"
        state.suspect_locations = [
            SuspectLocation(file_path="a.py", start_line=10, end_line=12, confidence=0.9)
        ]
        state.repair_plan = RepairPlan(issue_type="type_error")
        (Path(repo) / ".agent" / "runs" / "resume-001").mkdir(parents=True)
        save_repair_checkpoint(state, repo)

        # 创建新的 state（模拟重启）并恢复 checkpoint 字段
        new_state = RepairState(issue_input="test issue")
        new_state.repair_run_id = "resume-001"

        mixin = RepairPipelineMixin()
        checkpoint = load_repair_checkpoint(repo, "resume-001")
        assert checkpoint is not None

        mixin._restore_state_from_repair_checkpoint(new_state, checkpoint)

        assert new_state.retry_count == 1
        assert new_state.phase == "patch"
        assert len(new_state.suspect_locations) == 1
        assert new_state.repair_plan is not None
        assert new_state.repair_plan.issue_type == "type_error"

    def test_resume_repair_reenters_patch_loop(self, tmp_path):
        """有效 checkpoint 应跳过 parse/localize，并继续执行 patch loop。"""
        from src.orchestrator import Orchestrator
        from src.repair.checkpoint_load import save_repair_checkpoint
        from src.state import CandidatePatch, RepairPlan, RepairState, SuspectLocation

        repo = str(tmp_path)
        state = RepairState(issue_input="test issue")
        state.repair_run_id = "resume-002"
        state.retry_count = 1
        state.phase = "patch"
        state.feedback = "previous verifier feedback"
        state.suspect_locations = [
            SuspectLocation(file_path="a.py", start_line=10, end_line=12, confidence=0.9)
        ]
        state.repair_plan = RepairPlan(issue_type="type_error")
        state.blackboard_snapshot = {"entries": {"scratch:feedback": "previous verifier feedback"}}
        (Path(repo) / ".agent" / "runs" / "resume-002").mkdir(parents=True)
        save_repair_checkpoint(state, repo)

        class ResumeOrchestrator(Orchestrator):
            def __init__(self):
                super().__init__(None, None, None, use_pytest_verify=False)
                self._repo_root = repo
                self.patch_calls = 0

            def _snapshot_repo(self):
                return {}

            def _restore_repo_snapshot(self, snapshot):
                return None

            def _parse_issue(self, issue):
                raise AssertionError("resume path should skip parse")

            def _run_localize_and_retrieve(self, state):
                raise AssertionError("resume path should skip localize/retrieve")

            def _run_patcher(self, state):
                self.patch_calls += 1
                assert state.feedback == "previous verifier feedback"
                assert state.suspect_locations
                return [
                    CandidatePatch(
                        file_path="a.py",
                        original_lines="old",
                        patched_lines="new",
                    )
                ], {
                    "total_ms": 1,
                    "model_call_ms": 0,
                    "parse_apply_ms": 1,
                }

        orch = ResumeOrchestrator()
        result = orch.repair("test issue", repair_timeout_s=0, resume_run_id="resume-002")

        assert orch.patch_calls == 1
        assert result.status == "fixed"
        assert result.node_timings.get("verify_skipped") is True
