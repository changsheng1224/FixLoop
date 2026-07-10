"""sandbox_results 单测。"""

from src.harness.sandbox_manager import EXEC_TIMEOUT_EXIT_CODE, ExecResult
from src.harness.sandbox_results import (
    is_exec_timeout,
    verification_result_for_exec_timeout,
    verification_result_for_pip_failure,
)


class TestSandboxResults:
    def test_is_exec_timeout(self):
        assert is_exec_timeout(ExecResult(EXEC_TIMEOUT_EXIT_CODE, "", "timeout"))
        assert not is_exec_timeout(ExecResult(1, "", ""))

    def test_pip_failure_non_timeout(self):
        result = verification_result_for_pip_failure(
            "pip install: exit_code=1\nerr",
            ExecResult(1, "stderr from pip", ""),
            timeout_s=600,
        )
        assert not result.all_passed
        assert "exit_code=1" in result.failure_logs[0]
        assert result.build_log.startswith("pip install")

    def test_exec_timeout_includes_build_log(self):
        result = verification_result_for_exec_timeout(
            "pip install",
            600,
            ExecResult(EXEC_TIMEOUT_EXIT_CODE, "", "timeout after 600s"),
            build_log="pip install: exit_code=-1",
        )
        assert result.build_log == "pip install: exit_code=-1"
        assert "sandbox pip install timeout" in result.failure_logs[0]
