"""repair 退出码映射与配置预检单测。"""

from src.cli_exit_codes import (
    REPAIR_EXIT_CONFIG,
    REPAIR_EXIT_FAIL,
    REPAIR_EXIT_OK,
    REPAIR_EXIT_TIMEOUT,
    repair_config_error,
    repair_exit_code,
)
from src.state import CandidatePatch, RepairState


class TestRepairExitCode:
    def test_fixed_without_patches_returns_fail(self):
        state = RepairState(issue_input="x", status="fixed")
        assert repair_exit_code(state) == REPAIR_EXIT_FAIL

    def test_fixed_returns_ok(self):
        state = RepairState(
            issue_input="x",
            status="fixed",
            candidate_patches=[CandidatePatch(file_path="a.py")],
        )
        assert repair_exit_code(state) == REPAIR_EXIT_OK

    def test_patched_with_patches_returns_ok(self):
        state = RepairState(
            issue_input="x",
            status="patched",
            candidate_patches=[CandidatePatch(file_path="a.py")],
        )
        assert repair_exit_code(state) == REPAIR_EXIT_OK

    def test_patched_without_patches_returns_fail(self):
        state = RepairState(issue_input="x", status="patched")
        assert repair_exit_code(state) == REPAIR_EXIT_FAIL

    def test_failed_returns_fail(self):
        state = RepairState(issue_input="x", status="failed")
        assert repair_exit_code(state) == REPAIR_EXIT_FAIL

    def test_exhausted_returns_fail(self):
        state = RepairState(issue_input="x", status="exhausted")
        assert repair_exit_code(state) == REPAIR_EXIT_FAIL

    def test_timeout_status_returns_timeout_exit(self):
        state = RepairState(issue_input="x", status="timeout")
        assert repair_exit_code(state) == REPAIR_EXIT_TIMEOUT

    def test_timeout_via_node_timings(self):
        state = RepairState(
            issue_input="x",
            status="failed",
            node_timings={"repair_timeout": 180},
        )
        assert repair_exit_code(state) == REPAIR_EXIT_TIMEOUT

    def test_timeout_via_agent_errors(self):
        state = RepairState(
            issue_input="x",
            status="failed",
            agent_errors={"orchestrator": "repair timeout (180s)"},
        )
        assert repair_exit_code(state) == REPAIR_EXIT_TIMEOUT

    def test_timeout_takes_priority_over_fail(self):
        state = RepairState(
            issue_input="x",
            status="failed",
            node_timings={"repair_timeout": 60},
            agent_errors={"orchestrator": "repair timeout (60s)"},
        )
        assert repair_exit_code(state) == REPAIR_EXIT_TIMEOUT


class TestRepairConfigError:
    def test_missing_repo(self, tmp_path):
        missing = tmp_path / "no-such-repo"
        err = repair_config_error(str(missing), api_key="sk-test")
        assert err is not None
        assert "不存在" in err

    def test_missing_api_key(self, tmp_path):
        err = repair_config_error(str(tmp_path), api_key="")
        assert err is not None
        assert "DEEPSEEK_API_KEY" in err

    def test_ok_when_repo_exists_and_key_set(self, tmp_path, monkeypatch):
        monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-test")
        assert repair_config_error(str(tmp_path)) is None

    def test_reads_api_key_from_env(self, tmp_path, monkeypatch):
        monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-env")
        assert repair_config_error(str(tmp_path), api_key=None) is None

    def test_whitespace_only_api_key_is_invalid(self, tmp_path):
        err = repair_config_error(str(tmp_path), api_key="   ")
        assert err is not None
