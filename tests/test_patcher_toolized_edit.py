"""工具化 Patcher：磁盘 diff → CandidatePatch + edit mode 切换。"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

from src.agents.factory import create_repair_agent
from src.orchestrator import Orchestrator
from src.repair.edit_from_disk import patches_from_snapshot_diff
from src.state import RepairPlan, RepairState, SuspectLocation


class TestEditFromDisk:
    def test_diff_produces_candidate(self):
        before = {"a.py": "x = 1\n"}
        after = {"a.py": "x = 2\n"}
        patches = patches_from_snapshot_diff(before, after)
        assert len(patches) == 1
        assert patches[0].file_path == "a.py"
        assert "x = 1" in patches[0].original_lines
        assert "x = 2" in patches[0].patched_lines
        assert "--- a/a.py" in patches[0].diff
        assert "+++ b/a.py" in patches[0].diff

    def test_unchanged_skipped(self):
        snap = {"a.py": "same\n"}
        assert patches_from_snapshot_diff(snap, snap) == []


class TestPatcherFactoryToolized:
    def test_patcher_not_json_mode(self, tmp_path: Path):
        from agent_runtime.providers.clients import FakeModelClient
        from agent_runtime.workspace import WorkspaceContext

        ws = WorkspaceContext.build(str(tmp_path))
        agent = create_repair_agent(
            "patcher", FakeModelClient(["ok"]), ws, cwd=str(tmp_path)
        )
        assert agent.config.json_mode is False
        assert agent.config.max_steps >= 10
        assert "patch_file" in agent._system_prompt or "read_file" in agent._system_prompt


class TestPatcherToolizedOrchestrator:
    def test_toolized_path_uses_disk_diff(self, tmp_path: Path):
        repo = tmp_path / "repo"
        repo.mkdir()
        target = repo / "v.py"
        target.write_text("value = 1\n", encoding="utf-8")

        orch = Orchestrator(
            localizer=None,
            retriever=None,
            patcher=MagicMock(),
            verifier=None,
        )
        orch._repo_root = str(repo)
        orch._repair_ctx = None
        orch._merge_blackboard_for_patch = lambda state: None
        orch._patcher_prompt = lambda *a, **k: ("prompt", {})
        orch._snapshot_repo = MagicMock(
            side_effect=[
                {"v.py": "value = 1\n"},
                {"v.py": "value = 2\n"},
            ]
        )
        orch._run_agent = MagicMock(return_value=("done", {"total_ms": 12, "internal": {}}))

        # Simulate tool already wrote file
        target.write_text("value = 2\n", encoding="utf-8")

        state = RepairState(
            issue_input="bug",
            repair_plan=RepairPlan(issue_type="logic_error", suspect_files=["v.py"]),
            suspect_locations=[
                SuspectLocation(file_path="v.py", start_line=1, end_line=1, reason="r")
            ],
        )
        applied, meta = orch._run_patcher_toolized(state, "prompt", {})
        assert len(applied) == 1
        assert applied[0].file_path == "v.py"
        assert meta.get("edit_mode") == "tools"
        assert state.node_timings.get("patcher_edit_mode") == "tools"

    def test_edit_mode_env_json(self, monkeypatch):
        monkeypatch.setenv("FIXLOOP_PATCHER_EDIT_MODE", "json")
        assert Orchestrator._patcher_edit_mode() == "json"
        monkeypatch.setenv("FIXLOOP_PATCHER_EDIT_MODE", "tools")
        assert Orchestrator._patcher_edit_mode() == "tools"
