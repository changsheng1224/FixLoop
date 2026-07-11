"""验证 Strategy：Docker 沙箱 vs 本地 pytest。"""

from __future__ import annotations

import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from agent_runtime.tools import TIER_CONTAINER, TIER_HOST

from src.state import VerificationResult


@dataclass
class VerifyRun:
    """单次验证运行结果。"""

    result: VerificationResult
    elapsed_ms: int
    internal: dict
    error: str | None = None


class VerifyStrategy(Protocol):
    """验证后端协议。"""

    def run(self, repo_root: str, test_path: str = "") -> VerifyRun: ...


def _verify_cancelled_run(start: float) -> VerifyRun:
    from src.harness.sandbox_results import verification_result_for_user_cancel

    return VerifyRun(
        result=verification_result_for_user_cancel(),
        elapsed_ms=int((time.time() - start) * 1000),
        internal={"user_cancel": True},
        error="user_cancel",
    )


def _verify_from_sandbox_result(
    result: VerificationResult,
    internal: dict,
    start: float,
) -> VerifyRun:
    error = "user_cancel" if internal.get("user_cancel") else None
    return VerifyRun(
        result=result,
        elapsed_ms=int((time.time() - start) * 1000),
        internal=internal,
        error=error,
    )


class DockerVerifyStrategy:
    """Docker sandbox build + pytest。"""

    def run(self, repo_root: str, test_path: str = "", cancel_token=None) -> VerifyRun:
        from src.harness.sandbox_verify import SandboxNotAvailableError, assert_sandbox_available
        from src.tools.sandbox_tools import run_sandbox_verification

        t0 = time.time()
        try:
            assert_sandbox_available()
        except SandboxNotAvailableError as exc:
            elapsed_ms = int((time.time() - t0) * 1000)
            return VerifyRun(
                result=VerificationResult(
                    all_passed=False,
                    failure_logs=[f"Docker sandbox 不可用，请使用 --pytest 降级: {exc}"],
                ),
                elapsed_ms=elapsed_ms,
                internal={"execution_tier": TIER_HOST, "sandbox_unavailable": True, "error": str(exc)},
                error="sandbox_unavailable",
            )
        try:
            result, internal = run_sandbox_verification(
                repo_root,
                test_path=test_path,
                cancel_token=cancel_token,
            )
            internal["execution_tier"] = TIER_CONTAINER
            return _verify_from_sandbox_result(result, internal, t0)
        except Exception as exc:
            elapsed_ms = int((time.time() - t0) * 1000)
            return VerifyRun(
                result=VerificationResult(all_passed=False, failure_logs=[str(exc)]),
                elapsed_ms=elapsed_ms,
                internal={"execution_tier": TIER_CONTAINER},
                error=str(exc),
            )


class PytestVerifyStrategy:
    """宿主机 subprocess pytest（execution_tier=host）。"""

    def run(self, repo_root: str, test_path: str = "", cancel_token=None) -> VerifyRun:
        from agent_runtime.cancellation import CancelledError, run_with_cancellation
        from src.eval.runner import run_pytest

        del test_path

        def _run_pytest():
            return run_pytest(Path(repo_root))

        t0 = time.time()
        try:
            if cancel_token is not None:
                code, out = run_with_cancellation(_run_pytest, cancel_token)
            else:
                code, out = _run_pytest()
        except CancelledError:
            return _verify_cancelled_run(t0)
        elapsed_ms = int((time.time() - t0) * 1000)
        passed = code == 0
        if not passed:
            print(
                f"  [verifier] pytest 失败 (exit={code})\n",
                end="",
                file=sys.stderr,
                flush=True,
            )
        return VerifyRun(
            result=VerificationResult(
                all_passed=passed,
                failure_logs=[out[-2000:]] if out and not passed else [],
            ),
            elapsed_ms=elapsed_ms,
            internal={"pytest_ms": elapsed_ms, "execution_tier": TIER_HOST},
        )


def record_verify_timings(state, run: VerifyRun, *, log_sandbox: bool = False) -> None:
    """写入 RepairState.node_timings 并可选打印 sandbox 耗时。"""
    from src.repair.timing_schema import set_phase_ms

    set_phase_ms(state.node_timings, "verify", run.elapsed_ms, internal=run.internal)
    if run.error:
        state.agent_errors["verifier"] = run.error
    if log_sandbox and run.internal:
        print(
            f"  [verifier] sandbox: create={run.internal.get('container_create_ms', '?')}ms "
            f"tar={run.internal.get('tar_copy_ms', '?')}ms "
            f"pip={run.internal.get('pip_ms', '?')}ms "
            f"pytest={run.internal.get('pytest_ms', '?')}ms",
            file=sys.stderr,
            flush=True,
        )
