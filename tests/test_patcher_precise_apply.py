"""精确 apply + DISK GROUNDING + apply recovery 回归。"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

from src.orchestrator import Orchestrator
from src.prompts.patcher_task_builder import assemble_patcher_variables
from src.repair.disk_grounding import build_disk_grounding_block, collect_grounding_targets
from src.repair.patch_applier import apply_patch_to_text
from src.repair.precise_apply import apply_candidate_precise
from src.state import CandidatePatch, RepairPlan, RepairState, SuspectLocation


class TestPreciseApply:
    def test_exact_substring_replace_all(self):
        text = "a = 1\nb = 1\n"
        patch = CandidatePatch(
            file_path="x.py",
            original_lines="= 1",
            patched_lines="= 2",
        )
        out = apply_candidate_precise(text, patch)
        assert out == "a = 2\nb = 2\n"

    def test_unified_diff_via_patch_engine(self):
        text = "line1\nold\nline3\n"
        patch = CandidatePatch(
            file_path="x.py",
            diff="@@ -1,3 +1,3 @@\n line1\n-old\n+new\n line3\n",
        )
        out = apply_candidate_precise(text, patch)
        assert out is not None
        assert "new" in out
        assert "old" not in out

    def test_fuzzy_still_works_when_precise_misses(self):
        text = "    regex = r'^[\\w.@+-]+$'\n"
        patch = CandidatePatch(
            file_path="v.py",
            original_lines="    regex  =  r'^[\\w.@+-]+$'",
            patched_lines="    regex = r'^[\\w.@+-]+\\Z'",
        )
        out = apply_patch_to_text(text, patch)
        assert out is not None
        assert "\\Z" in out


class TestDiskGrounding:
    def test_collect_and_render(self, tmp_path: Path):
        f = tmp_path / "mod.py"
        f.write_text("alpha\nbeta\ngamma\n", encoding="utf-8")

        def reader(path: str, start: int, end: int) -> str:
            lines = f.read_text(encoding="utf-8").splitlines()
            return "\n".join(lines[start - 1 : end])

        suspects = [
            SuspectLocation(file_path="mod.py", start_line=2, end_line=2, reason="x")
        ]
        targets = collect_grounding_targets(suspects, plan_files=["mod.py"])
        block = build_disk_grounding_block(targets, reader, context_lines=1)
        assert "DISK GROUNDING" in block
        assert "2|beta" in block
        assert "1|alpha" in block

    def test_assemble_includes_grounding(self, tmp_path: Path):
        def read_snippet(path, s, e):
            return "snippet"

        def read_range(path, s, e):
            return "x = 1"

        def read_tests(*_a, **_k):
            return []

        def fallback(_plan, _issue):
            return []

        plan = RepairPlan(issue_type="logic_error", suspect_files=["a.py"])
        suspects = [
            SuspectLocation(file_path="a.py", start_line=1, end_line=1, reason="r")
        ]
        variables, _, _ = assemble_patcher_variables(
            suspects=suspects,
            context=None,
            feedback="",
            plan=plan,
            issue="bug",
            read_snippet=read_snippet,
            read_test_context=read_tests,
            fallback_suspects=fallback,
            read_line_range=read_range,
        )
        assert "DISK GROUNDING" in variables["disk_grounding_block"]
        assert "1|x = 1" in variables["disk_grounding_block"]


class TestApplyRecovery:
    def test_recovery_applies_after_bad_first_patch(self, tmp_path: Path):
        repo = tmp_path / "repo"
        repo.mkdir()
        target = repo / "v.py"
        target.write_text("value = 1\n", encoding="utf-8")

        good = (
            '[{"file_path":"v.py","original_lines":"value = 1",'
            '"patched_lines":"value = 2"}]'
        )

        patcher = MagicMock()
        patcher.complete_once.return_value = good

        orch = Orchestrator(
            localizer=None,
            retriever=None,
            patcher=patcher,
            verifier=None,
        )
        orch._repo_root = str(repo)
        orch._repair_ctx = None

        state = RepairState(
            issue_input="bug",
            repair_plan=RepairPlan(issue_type="logic_error", suspect_files=["v.py"]),
        )
        # Seed failed apply error as production path would
        state.agent_errors["patcher_apply"] = "hunk_mismatch:v.py"
        applied = orch._recover_failed_apply(
            state,
            prompt="base prompt with DISK GROUNDING",
            patcher_system="sys",
            apply_detail="hunk_mismatch:v.py: wanted `value = 999`",
            max_attempts=1,
        )
        assert applied
        assert target.read_text(encoding="utf-8") == "value = 2\n"
        assert state.node_timings.get("patcher_apply_recovered") == 1
        assert patcher.complete_once.call_count == 1
        recovery_user = patcher.complete_once.call_args[0][0]
        assert "APPLY FAILED" in recovery_user
        assert "DISK GROUNDING" in recovery_user or "exact" in recovery_user.lower()
