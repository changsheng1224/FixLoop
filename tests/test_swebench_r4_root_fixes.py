"""R4 根因修复回归：E16 shell exec / E17 FAIL_TO_PASS / E18 baseline parse retry."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

from src.benchmark.swebench.convert import extract_fail_to_pass_hints, instance_to_issue
from src.benchmark.swebench.types import SweInstance
from src.harness.python_runner import PythonTestRunner
from src.harness.sandbox_manager import ExecResult, sandbox_pip_install_command, sandbox_shell_argv
from src.orchestrator import Orchestrator
from src.repair.baseline_apply import apply_baseline_answer
from src.repair.failure_tags import FailureTag, classify_failure_tags
from src.repair.termination import RepairTerminalStatus
from src.state import CandidatePatch, RepairState, RetrievedContext, VerificationResult
from tests.test_sandbox_manager import FakeManager, FakeSandbox


class TestE16ShellExec:
    def test_shell_argv_keeps_ampersand_and_user_flag_in_one_string(self):
        cmd = sandbox_pip_install_command()
        argv = sandbox_shell_argv(cmd)
        assert argv[0:2] == ["/bin/sh", "-c"]
        # 整段仍是一个 argv，mkdir 不会单独吃到 --user
        assert "--user" in argv[2]
        assert argv[2].startswith("mkdir -p")
        assert "&&" in argv[2]


class TestE17FailToPass:
    def test_extract_fail_to_pass_hints_from_issue(self):
        inst = SweInstance(
            instance_id="demo__demo-1",
            repo="demo/demo",
            base_commit="abc",
            problem_statement="bug",
            FAIL_TO_PASS=["pkg/tests/test_x.py::test_a", "pkg/tests/test_y.py::test_b"],
        )
        issue = instance_to_issue(inst)
        hints = extract_fail_to_pass_hints(issue)
        assert hints == [
            "pkg/tests/test_x.py::test_a",
            "pkg/tests/test_y.py::test_b",
        ]

    def test_pick_test_path_falls_back_to_fail_to_pass(self):
        orch = Orchestrator.__new__(Orchestrator)
        state = RepairState(
            issue_input=instance_to_issue(
                SweInstance(
                    instance_id="demo__demo-1",
                    repo="demo/demo",
                    base_commit="abc",
                    problem_statement="bug",
                    FAIL_TO_PASS=["tests/test_demo.py::test_bug"],
                )
            ),
            retrieved_context=RetrievedContext(related_tests=[]),
        )
        assert orch._pick_test_path(state) == "tests/test_demo.py::test_bug"

    def test_empty_collection_tagged_verify_config(self):
        mgr = FakeManager(
            {
                "pytest": ExecResult(0, "collected 0", ""),
                "cat /code/.report.json": ExecResult(
                    0,
                    '{"summary": {"total": 0, "passed": 0, "failed": 0, "error": 0}, "tests": []}',
                    "",
                ),
            }
        )
        runner = PythonTestRunner(mgr)
        result = runner.run(FakeSandbox(), test_path="missing/test.py")
        assert result.total_tests == 0
        assert not result.all_passed
        assert any("verify_config:" in log for log in result.failure_logs)
        assert "missing/test.py" in result.failure_logs[0]

    def test_verify_config_tag_when_patches_exist(self):
        state = RepairState(
            issue_input="x",
            status=RepairTerminalStatus.EXHAUSTED,
            retry_count=3,
            candidate_patches=[CandidatePatch(file_path="a.py")],
            verification_result=VerificationResult(
                all_passed=False,
                total_tests=0,
                failure_logs=["verify_config: 未收集到任何测试 (target=.)"],
            ),
        )
        assert classify_failure_tags(state) == [FailureTag.VERIFY_CONFIG]


class TestE18BaselineParseRetry:
    def test_schema_retry_recovers_patches(self, tmp_path: Path):
        repo = tmp_path / "repo"
        repo.mkdir()
        target = repo / "mod.py"
        target.write_text("old\n", encoding="utf-8")

        bad = "I will fix it somehow without JSON."
        good = (
            '[{"file_path": "mod.py", "original_lines": "old\\n", '
            '"patched_lines": "new\\n"}]'
        )

        agent = MagicMock()
        agent.ask.return_value = bad
        agent.complete_once.return_value = good

        state = RepairState(issue_input="bug")
        apply_baseline_answer(
            agent,
            str(repo),
            "please fix",
            state,
            mark_fixed_on_apply=True,
        )
        assert state.candidate_patches
        assert state.candidate_patches[0].file_path == "mod.py"
        assert state.node_timings.get("baseline_parse_recovered") is True
        assert target.read_text(encoding="utf-8") == "new\n"
        agent.complete_once.assert_called_once()
