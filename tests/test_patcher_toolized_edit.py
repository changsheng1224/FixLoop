"""工具化 Patcher：磁盘 diff → CandidatePatch。"""

from __future__ import annotations

import json
import time
from pathlib import Path

from agent_runtime.providers.clients import FakeModelClient
from agent_runtime.workspace import WorkspaceContext
from src.agents.factory import create_repair_agent
from src.orchestrator import Orchestrator
from src.repair.execution.edit_from_disk import patches_from_snapshot_diff
from src.repair.run_context import RepairRunContext
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
        ws = WorkspaceContext.build(str(tmp_path))
        agent = create_repair_agent("patcher", FakeModelClient(["ok"]), ws, cwd=str(tmp_path))
        assert agent.config.json_mode is False
        assert agent.config.max_steps >= 10
        assert "patch_file" in agent._system_prompt or "read_file" in agent._system_prompt


class TestPatcherToolizedOrchestrator:
    def test_toolized_path_uses_disk_diff(self, tmp_path: Path):
        repo = tmp_path / "repo"
        repo.mkdir()
        target = repo / "v.py"
        target.write_text("value = 1\n", encoding="utf-8")

        client = FakeModelClient(
            [
                '{"conclusion":"v.py initializes value to 1; change it to 2."}',
                '<tool>{"name":"read_file","args":{"path":"v.py"}}</tool>',
                "<tool>"
                + json.dumps(
                    {
                        "name": "patch_file",
                        "args": {"path": "v.py", "old_text": "value = 1", "new_text": "value = 2"},
                    }
                )
                + "</tool>",
                "<final>Updated value in v.py.</final>",
            ]
        )
        agent = create_repair_agent(
            "patcher", client, WorkspaceContext.build(str(repo)), cwd=str(repo)
        )
        orch = Orchestrator(agent, verifier=None, sandbox_policy="disabled")
        orch._repair_ctx = RepairRunContext(repair_started_at=time.time())
        orch._merge_blackboard_for_patch = lambda state: None

        state = RepairState(
            issue_input="Change the initial value in v.py from 1 to 2.",
            repair_run_id="toolized-disk-diff-run",
            repair_plan=RepairPlan(issue_type="logic_error", suspect_files=["v.py"]),
            suspect_locations=[
                SuspectLocation(file_path="v.py", start_line=1, end_line=1, reason="r")
            ],
        )
        agent.shared_run_id = state.repair_run_id
        try:
            applied, meta = orch._run_patcher_toolized(state, state.issue_input, {})
            assert len(applied) == 1, (meta, state.agent_errors)
            assert applied[0].file_path == "v.py"
            assert applied[0].original_lines == "value = 1\n"
            assert applied[0].patched_lines == target.read_text(encoding="utf-8") == "value = 2\n"
            assert "--- a/v.py" in applied[0].diff and "+++ b/v.py" in applied[0].diff
            assert meta.get("edit_mode") == "tools"
            assert state.node_timings.get("patcher_edit_mode") == "tools"
            assert state.node_timings["patcher_write_attempted"]
            session = orch._plan_binding.session
            assert session.plan.node("edit").status == "succeeded"
            writes = [
                op
                for op in session.store.latest("operation", "operation_id").values()
                if op["effect"] == "write"
            ]
            assert len(writes) == 1 and writes[0]["status"] == "success"
        finally:
            if orch._plan_binding:
                orch._plan_binding.close()
