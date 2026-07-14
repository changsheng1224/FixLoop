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


TIER_NONE = "none"
TIER_STATIC = "static"
ISOLATION_CONTAINER = "container"
ISOLATION_TRUSTED_LOCAL = "trusted_local"
ISOLATION_NON_EXECUTING = "non_executing"
ISOLATION_NONE = "none"
HOST_FALLBACK_WARNING = "Host fallback has no sandbox isolation; run only trusted code."
STATIC_FALLBACK_WARNING = "Static verification does not execute tests."


def _tier_internal(
    *,
    requested_tier: str,
    actual_tier: str,
    isolation_level: str,
    trusted_execution: bool,
    warning: str = "",
    **extra,
) -> dict:
    data = {
        "execution_tier": actual_tier if actual_tier != TIER_NONE else requested_tier,
        "requested_tier": requested_tier,
        "actual_tier": actual_tier,
        "isolation_level": isolation_level,
        "trusted_execution": trusted_execution,
    }
    if warning:
        data["warning"] = warning
    data.update(extra)
    return data


def _verify_cancelled_run(start: float) -> VerifyRun:
    from src.harness.sandbox_results import verification_result_for_user_cancel

    return VerifyRun(
        result=verification_result_for_user_cancel(),
        elapsed_ms=int((time.time() - start) * 1000),
        internal=_tier_internal(
            requested_tier=TIER_NONE,
            actual_tier=TIER_NONE,
            isolation_level=ISOLATION_NONE,
            trusted_execution=False,
            user_cancel=True,
        ),
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
                internal=_tier_internal(
                    requested_tier=TIER_CONTAINER,
                    actual_tier=TIER_NONE,
                    isolation_level=ISOLATION_NONE,
                    trusted_execution=False,
                    sandbox_unavailable=True,
                    fallback_candidate=TIER_HOST,
                    error=str(exc),
                ),
                error="sandbox_unavailable",
            )
        try:
            result, internal = run_sandbox_verification(
                repo_root,
                test_path=test_path,
                cancel_token=cancel_token,
            )
            internal.update(
                _tier_internal(
                    requested_tier=TIER_CONTAINER,
                    actual_tier=TIER_CONTAINER,
                    isolation_level=ISOLATION_CONTAINER,
                    trusted_execution=False,
                )
            )
            return _verify_from_sandbox_result(result, internal, t0)
        except Exception as exc:
            elapsed_ms = int((time.time() - t0) * 1000)
            return VerifyRun(
                result=VerificationResult(all_passed=False, failure_logs=[str(exc)]),
                elapsed_ms=elapsed_ms,
                internal=_tier_internal(
                    requested_tier=TIER_CONTAINER,
                    actual_tier=TIER_CONTAINER,
                    isolation_level=ISOLATION_CONTAINER,
                    trusted_execution=False,
                ),
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
            internal=_tier_internal(
                requested_tier=TIER_HOST,
                actual_tier=TIER_HOST,
                isolation_level=ISOLATION_TRUSTED_LOCAL,
                trusted_execution=True,
                warning=HOST_FALLBACK_WARNING,
                pytest_ms=elapsed_ms,
            ),
        )


class StaticVerifyStrategy:
    """非执行静态验证：只编译 Python 文件，不运行项目测试。"""

    def run(self, repo_root: str, test_path: str = "", cancel_token=None) -> VerifyRun:
        del test_path, cancel_token
        root = Path(repo_root)
        t0 = time.time()
        failures: list[str] = []
        for path in root.rglob("*.py"):
            if any(part in {".git", ".pytest_cache", "__pycache__"} for part in path.parts):
                continue
            try:
                source = path.read_text(encoding="utf-8")
                compile(source, str(path), "exec")
            except (OSError, SyntaxError, UnicodeDecodeError) as exc:
                try:
                    rel = path.relative_to(root)
                except ValueError:
                    rel = path
                failures.append(f"{rel}: {exc}")
        elapsed_ms = int((time.time() - t0) * 1000)
        ok = not failures
        return VerifyRun(
            result=VerificationResult(
                all_passed=ok,
                total_tests=0,
                passed=0,
                failed=0 if ok else 1,
                failure_logs=failures,
            ),
            elapsed_ms=elapsed_ms,
            internal=_tier_internal(
                requested_tier=TIER_STATIC,
                actual_tier=TIER_STATIC,
                isolation_level=ISOLATION_NON_EXECUTING,
                trusted_execution=False,
                warning=STATIC_FALLBACK_WARNING,
                static_ms=elapsed_ms,
            ),
            error=None if ok else "static_verify_failed",
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
