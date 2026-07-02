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
        target = test_path.strip() if test_path else "."
        if target.startswith("/code/"):
            target = target[len("/code/"):]
        pytest_target = f"/code/{target}" if target != "." else "/code"
        # Step 1: 运行测试（构建已在 sandbox_build 中完成）
        test_cmd = (
            f"pytest {pytest_target} --json-report "
            f"--json-report-file=/code/.report.json -v"
        )
        test = self.manager.execute(sandbox, f"/entrypoint.sh test {test_cmd}", timeout=600)

        # 尝试解析 JSON 报告
        try:
            report_data = self._read_report(sandbox)
            return self._parse_report(report_data)
        except Exception:
            pass

        # 降级：无法解析 JSON 时不视为通过
        logs = [test.stdout[-500:]] if test.stdout else []
        if test.exit_code == 0:
            logs = ["pytest 退出码为 0，但未生成可解析的 JSON 报告（可能未收集到测试）"]
        return VerificationResult(
            all_passed=False,
            total_tests=0,
            passed=0,
            failed=max(test.exit_code, 1),
            failure_logs=logs,
        )

    def _read_report(self, sandbox) -> dict:
        """读取容器内的 .report.json。"""
        result = self.manager.execute(sandbox, "cat /code/.report.json", timeout=10)
        raw = (result.stdout or "").strip()
        if not raw or result.exit_code != 0:
            return {}
        try:
            data, _ = json.JSONDecoder().raw_decode(raw)
            return data if isinstance(data, dict) else {}
        except json.JSONDecodeError:
            return {}

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
            all_passed=(total > 0 and failed == 0 and error == 0),
            total_tests=total,
            passed=passed,
            failed=failed,
            error=error,
            failure_logs=failure_logs or (
                ["未收集到任何测试"] if total == 0 else []
            ),
        )
