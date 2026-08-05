"""PythonTestRunner：容器内测试执行与结果解析。

默认 pytest --json-report；Django 源码树走 tests/runtests.py。
"""

import json

from src.harness.sandbox_manager import TEST_TIMEOUT_S
from src.harness.sandbox_results import (
    is_exec_cancelled,
    is_exec_timeout,
    verification_result_for_exec_timeout,
    verification_result_for_user_cancel,
)
from src.state import VerificationResult


class PythonTestRunner:
    """Python 测试运行器。"""

    def __init__(self, sandbox_manager):
        self.manager = sandbox_manager

    def run(
        self,
        sandbox,
        test_path: str = "",
        cancel_token=None,
        *,
        env_prefix: str = "",
        profile=None,
        repo_path: str = "",
    ) -> VerificationResult:
        """在容器内构建并运行测试。"""
        if cancel_token is not None and cancel_token.is_cancelled:
            return verification_result_for_user_cancel()

        from src.harness.verify_env import detect_verify_profile

        prof = profile or detect_verify_profile(repo_path or None)
        if getattr(prof, "kind", "") == "django_runtests":
            return self._run_django_runtests(
                sandbox,
                test_path,
                cancel_token=cancel_token,
                env_prefix=env_prefix,
                profile=prof,
            )
        return self._run_pytest(
            sandbox,
            test_path,
            cancel_token=cancel_token,
            env_prefix=env_prefix,
        )

    def _run_django_runtests(
        self,
        sandbox,
        test_path: str,
        *,
        cancel_token=None,
        env_prefix: str = "",
        profile,
    ) -> VerificationResult:
        from src.harness.verify_env import (
            build_django_runtests_command,
            django_labels_from_target,
            parse_django_runtests_output,
        )

        labels = django_labels_from_target(test_path)
        if not labels:
            return VerificationResult(
                all_passed=False,
                total_tests=0,
                failure_logs=[
                    "verify_config: django runtests 需要具体 test label "
                    "（禁止空 target 跑全量）；请提供 FAIL_TO_PASS / related_tests / 失败 nodeid。"
                ],
            )

        cmd = build_django_runtests_command(
            labels,
            runtests_path=getattr(profile, "runtests_path", "") or "tests/runtests.py",
            settings_module=getattr(profile, "settings_module", "") or "",
        )
        prefix = f"{env_prefix} && " if env_prefix.strip() else ""
        # runtests 自行管理 django；经 entrypoint test 包装以统一超时/日志
        test = self.manager.execute(
            sandbox,
            f"{prefix}/entrypoint.sh test {cmd}",
            timeout=TEST_TIMEOUT_S,
            cancel_token=cancel_token,
        )
        if is_exec_cancelled(test):
            return verification_result_for_user_cancel()
        if is_exec_timeout(test):
            return verification_result_for_exec_timeout("django_runtests", TEST_TIMEOUT_S, test)

        return parse_django_runtests_output(
            test.stdout or "",
            exit_code=int(test.exit_code or 1),
            labels=labels,
        )

    def _run_pytest(
        self,
        sandbox,
        test_path: str = "",
        cancel_token=None,
        *,
        env_prefix: str = "",
    ) -> VerificationResult:
        target = test_path.strip() if test_path else "."
        if target.startswith("/code/"):
            target = target[len("/code/") :]
        pytest_target = f"/code/{target}" if target != "." else "/code"
        test_cmd = f"pytest {pytest_target} --json-report --json-report-file=/code/.report.json -v"
        prefix = f"{env_prefix} && " if env_prefix.strip() else ""
        test = self.manager.execute(
            sandbox,
            f"{prefix}/entrypoint.sh test {test_cmd}",
            timeout=TEST_TIMEOUT_S,
            cancel_token=cancel_token,
        )

        if is_exec_cancelled(test):
            return verification_result_for_user_cancel()

        if is_exec_timeout(test):
            return verification_result_for_exec_timeout("pytest", TEST_TIMEOUT_S, test)

        report_data: dict = {}
        try:
            report_data = self._read_report(sandbox, cancel_token=cancel_token)
            result = self._parse_report(report_data, target=target)
        except Exception:
            result = None

        if result is not None:
            if result.total_tests == 0:
                tail = (test.stdout or "").strip()[-800:]
                if tail and not any(tail[:80] in log for log in result.failure_logs):
                    result.failure_logs = list(result.failure_logs) + [
                        f"pytest_stdout_tail:\n{tail}"
                    ]
            return result

        logs = [test.stdout[-800:]] if test.stdout else []
        if test.exit_code == 0:
            logs = [
                "verify_config: pytest 退出码为 0，但未生成可解析的 JSON 报告"
                f"（target={target}；可能未收集到测试）"
            ]
            if test.stdout:
                logs.append(f"pytest_stdout_tail:\n{test.stdout.strip()[-800:]}")
        return VerificationResult(
            all_passed=False,
            total_tests=0,
            passed=0,
            failed=max(test.exit_code, 1),
            failure_logs=logs,
        )

    def _read_report(self, sandbox, cancel_token=None) -> dict:
        """读取容器内的 .report.json。"""
        result = self.manager.execute(
            sandbox, "cat /code/.report.json", timeout=10, cancel_token=cancel_token
        )
        if is_exec_cancelled(result):
            return {}
        raw = (result.stdout or "").strip()
        if not raw or result.exit_code != 0:
            return {}
        try:
            data, _ = json.JSONDecoder().raw_decode(raw)
            return data if isinstance(data, dict) else {}
        except json.JSONDecodeError:
            return {}

    def _parse_report(self, data: dict, *, target: str = ".") -> VerificationResult:
        """解析 pytest-json-report 输出。"""
        summary = data.get("summary", {})
        total = summary.get("total", 0) or data.get("total", 0)
        passed = summary.get("passed", 0) or data.get("passed", 0)
        failed = summary.get("failed", 0) or data.get("failed", 0)
        error = summary.get("error", 0) or data.get("error", 0)

        failure_logs = []
        for test in data.get("tests", []):
            if test.get("outcome") in ("failed", "error"):
                name = test.get("nodeid", test.get("name", ""))
                msg = test.get("call", {}).get("longrepr", "")
                failure_logs.append(f"{name}: {msg[:200]}")

        if total == 0 and not failure_logs:
            failure_logs = [
                f"verify_config: 未收集到任何测试 (target={target})。"
                "请检查 related_tests / FAIL_TO_PASS 路径是否存在，而非仅改业务代码。"
            ]

        return VerificationResult(
            all_passed=(total > 0 and failed == 0 and error == 0),
            total_tests=total,
            passed=passed,
            failed=failed,
            error=error,
            failure_logs=failure_logs,
        )
