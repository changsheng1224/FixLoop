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
        """resume_run_id 有效时 _repair_impl 跳过 parse 直接返回 state。"""
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

        # 创建新的 state（模拟重启）
        new_state = RepairState(issue_input="test issue")
        new_state.repair_run_id = "resume-001"

        from unittest.mock import MagicMock

        mixin = RepairPipelineMixin()
        mixin._repo_root = repo
        mixin.retriever = None
        mixin._snapshot_repo = MagicMock(return_value={})

        result = mixin._repair_impl(new_state)
        # resume 路径直接返回 state（跳过 parse/localize）
        assert result.retry_count == 1
        assert result.phase == "patch"
        assert len(result.suspect_locations) == 1
