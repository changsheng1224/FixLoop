"""PythonTestRunner：容器内 pytest 执行与结果解析。

使用 pytest --json-report 输出结构化 JSON，避免正则解析 pytest 输出。
"""

import json

from src.state import VerificationResult


class PythonTestRunner:
    """Python 测试运行器。"""

    def __init__(self, sandbox_manager):
        self.manager = sandbox_manager

    def run(self, sandbox, test_path: str = "") -> VerificationResult:
        """在容器内构建并运行测试。

        Args:
            sandbox: Sandbox 实例。
            test_path: 测试路径（空 = tests/）。

        Returns:
            VerificationResult 实例。
        """
        # Step 1: 构建
        build = self.manager.execute(
            sandbox,
            "/entrypoint.sh build pip install -e /code",
            timeout=300,
        )
        if build.exit_code != 0:
            return VerificationResult(
                all_passed=False,
                build_log=build.stdout or build.stderr or "构建失败",
            )

        # Step 2: 运行测试
        test_cmd = f"pytest /code/{test_path or 'tests/'} --json-report -v"
        test = self.manager.execute(sandbox, f"/entrypoint.sh test {test_cmd}", timeout=600)

        # 尝试解析 JSON 报告
        try:
            report_data = self._read_report(sandbox)
            return self._parse_report(report_data)
        except Exception:
            pass

        # 降级：从 stdout 中提取基础信息
        return VerificationResult(
            all_passed=test.exit_code == 0,
            total_tests=0,
            passed=0,
            failed=test.exit_code,
            failure_logs=[test.stdout[-500:]] if test.exit_code != 0 else [],
        )

    def _read_report(self, sandbox) -> dict:
        """读取容器内的 .report.json。"""
        result = self.manager.execute(
            sandbox, "cat /code/.report.json 2>/dev/null || echo '{}'", timeout=10,
        )
        return json.loads(result.stdout or "{}")

    def _parse_report(self, data: dict) -> VerificationResult:
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

        return VerificationResult(
            all_passed=(failed == 0 and error == 0),
            total_tests=total,
            passed=passed,
            failed=failed,
            error=error,
            failure_logs=failure_logs,
        )
