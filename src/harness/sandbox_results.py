"""Sandbox exec 结果 → VerificationResult 映射（超时 / 构建失败）。"""

from __future__ import annotations

from src.harness.sandbox_manager import EXEC_TIMEOUT_EXIT_CODE, ExecResult
from src.state import VerificationResult


def is_exec_timeout(result: ExecResult) -> bool:
    return result.exit_code == EXEC_TIMEOUT_EXIT_CODE


def verification_result_for_exec_timeout(
    phase: str,
    timeout_s: int,
    result: ExecResult,
    *,
    build_log: str = "",
) -> VerificationResult:
    """``execute`` 线程超时时生成明确 failure_logs（供 Patcher 反馈）。"""
    logs = [f"sandbox {phase} timeout after {timeout_s}s"]
    if result.stderr:
        logs.append(result.stderr.strip())
    if result.stdout:
        tail = result.stdout[-500:].strip()
        if tail:
            logs.append(tail)
    return VerificationResult(
        all_passed=False,
        total_tests=0,
        passed=0,
        failed=1,
        failure_logs=logs,
        build_log=build_log,
    )


def verification_result_for_pip_failure(
    build_result: str,
    pip_exec: ExecResult,
    *,
    timeout_s: int,
) -> VerificationResult:
    """pip install 非零退出（含超时）时跳过 pytest。"""
    if is_exec_timeout(pip_exec):
        return verification_result_for_exec_timeout(
            "pip install",
            timeout_s,
            pip_exec,
            build_log=build_result,
        )
    logs = [f"sandbox pip install failed: exit_code={pip_exec.exit_code}"]
    if pip_exec.stdout:
        tail = pip_exec.stdout[-500:].strip()
        if tail:
            logs.append(tail)
    return VerificationResult(
        all_passed=False,
        total_tests=0,
        passed=0,
        failed=1,
        failure_logs=logs,
        build_log=build_result,
    )
