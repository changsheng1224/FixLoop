"""验证 Strategy：Docker 沙箱 vs 本地 pytest。"""

from __future__ import annotations

import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

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


class DockerVerifyStrategy:
    """Docker sandbox build + pytest。"""

    def run(self, repo_root: str, test_path: str = "") -> VerifyRun:
        from src.tools.sandbox_tools import run_sandbox_verification

        t0 = time.time()
        try:
            result, internal = run_sandbox_verification(repo_root, test_path=test_path)
            elapsed_ms = int((time.time() - t0) * 1000)
            return VerifyRun(result=result, elapsed_ms=elapsed_ms, internal=internal)
        except Exception as exc:
            elapsed_ms = int((time.time() - t0) * 1000)
            return VerifyRun(
                result=VerificationResult(all_passed=False, failure_logs=[str(exc)]),
                elapsed_ms=elapsed_ms,
                internal={},
                error=str(exc),
            )


class PytestVerifyStrategy:
    """宿主机 subprocess pytest。"""

    def run(self, repo_root: str, test_path: str = "") -> VerifyRun:
        from src.eval.runner import run_pytest

        del test_path
        t0 = time.time()
        code, out = run_pytest(Path(repo_root))
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
            internal={"pytest_ms": elapsed_ms},
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
