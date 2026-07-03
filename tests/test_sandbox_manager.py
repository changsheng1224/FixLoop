"""SandboxManager + PatchApplier + TestRunner 单测（Mock docker）。"""

import json

from src.harness.patch_applier import PatchApplier
from src.harness.python_runner import PythonTestRunner
from src.harness.sandbox_manager import ExecResult, SandboxManager
from src.state import CandidatePatch


class FakeSandbox:
    """模拟 Sandbox（不依赖 Docker）。"""

    id = "fake-id"
    profile = "python"


class FakeManager:
    """模拟 SandboxManager。"""

    def __init__(self, responses: dict[str, ExecResult] = None):
        self.responses = responses or {}
        self.calls = []

    def execute(self, sandbox, command: str, timeout: int = 600) -> ExecResult:
        self.calls.append(command)
        for key, result in self.responses.items():
            if key in command:
                return result
        return ExecResult(exit_code=0, stdout="ok", stderr="")


class TestSandboxManager:
    def test_create_returns_sandbox(self):
        mgr = SandboxManager()
        # _docker 未初始化时不应报错
        assert mgr.IMAGE == "repair-agent/python-repair"


class TestPatchApplier:
    def test_apply_single_patch(self):
        mgr = FakeManager({"patch": ExecResult(0, "patched", "")})
        applier = PatchApplier(mgr)
        patch = CandidatePatch(file_path="calc.py", diff="-old\n+new")
        results = applier.apply(FakeSandbox(), [patch])
        assert results == [True]

    def test_apply_rollback_on_failure(self):
        mgr = FakeManager({"patch_0": ExecResult(1, "", "fail")})
        applier = PatchApplier(mgr)
        patch = CandidatePatch(file_path="calc.py", diff="-old\n+new")
        results = applier.apply(FakeSandbox(), [patch])
        assert results == [False]


class TestPythonRunner:
    def test_parse_json_report(self):
        mgr = FakeManager(
            {
                "pip install": ExecResult(0, "installed", ""),
                "pytest": ExecResult(0, "tests ran", ""),
                "cat /code/.report.json": ExecResult(
                    0,
                    json.dumps(
                        {
                            "summary": {"total": 3, "passed": 2, "failed": 1, "error": 0},
                            "tests": [
                                {"nodeid": "test_add", "outcome": "passed"},
                                {
                                    "nodeid": "test_sub",
                                    "outcome": "failed",
                                    "call": {"longrepr": "AssertionError: 3!=5"},
                                },
                            ],
                        }
                    ),
                    "",
                ),
            }
        )
        runner = PythonTestRunner(mgr)
        result = runner.run(FakeSandbox())
        assert result.total_tests == 3
        assert result.passed == 2
        assert result.failed == 1
        assert not result.all_passed

    def test_pytest_timeout_not_passed(self):
        """exec 超时时不应误判为通过。"""
        mgr = FakeManager(
            {
                "pytest": ExecResult(-1, "", "timeout after 900s"),
            }
        )
        runner = PythonTestRunner(mgr)
        result = runner.run(FakeSandbox())
        assert not result.all_passed
        assert result.total_tests == 0
        assert result.failure_logs

    def test_pytest_exit_zero_without_report_not_passed(self):
        """pytest 0 退出但无 JSON 报告时 all_passed=False。"""
        mgr = FakeManager(
            {
                "pytest": ExecResult(0, "no tests ran", ""),
                "cat /code/.report.json": ExecResult(1, "", "No such file"),
            }
        )
        runner = PythonTestRunner(mgr)
        result = runner.run(FakeSandbox())
        assert not result.all_passed
        assert result.total_tests == 0
