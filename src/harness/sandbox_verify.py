"""Sandbox 验证编排：create → pip → pytest → destroy。"""

from __future__ import annotations

import time
from pathlib import Path

from src.harness.python_runner import PythonTestRunner
from src.harness.sandbox_manager import (
    BUILD_TIMEOUT_S,
    Sandbox,
    SandboxManager,
    sandbox_pip_install_command,
    ExecResult,
)
from src.harness.sandbox_results import verification_result_for_pip_failure
from src.harness.sandbox_tar import SandboxArchiveError
from src.state import VerificationResult


def run_sandbox_verification_flow(
    context,
    repo: str,
    test_path: str,
    cancel_token=None,
) -> tuple[VerificationResult, dict]:
    """创建容器 → 可选 pip install → pytest → 销毁。"""
    from src.harness.sandbox_results import verification_result_for_user_cancel

    if cancel_token is not None and cancel_token.is_cancelled:
        return verification_result_for_user_cancel(), {"user_cancel": True}

    sandbox_id = getattr(context, "_sandbox_id", None)
    mgr = getattr(context, "_sandbox_mgr", None)
    timings: dict[str, int | str] = {}
    sandbox = None
    created_here = False

    def _abort_if_cancelled() -> tuple[VerificationResult, dict] | None:
        if cancel_token is not None and cancel_token.is_cancelled:
            if sandbox is not None and mgr is not None and created_here:
                try:
                    mgr.destroy(sandbox)
                except Exception:
                    pass
                context._sandbox_id = None
                context._sandbox_mgr = None
            timings["user_cancel"] = True
            return verification_result_for_user_cancel(), timings
        return None

    try:
        if sandbox_id is None or mgr is None:
            aborted = _abort_if_cancelled()
            if aborted is not None:
                return aborted
            mgr = SandboxManager()
            try:
                sandbox = mgr.create(repo)
            except SandboxArchiveError as exc:
                return verification_result_for_tar_error(exc), timings_for_tar_error(exc)
            created_here = True
            context._sandbox_id = sandbox.id
            context._sandbox_mgr = mgr
            context._sandbox_repo = repo
            if sandbox.timings:
                timings.update(sandbox.timings)
            aborted = _abort_if_cancelled()
            if aborted is not None:
                return aborted
            build_result, pip_ms, pip_exec = maybe_pip_install(
                mgr, sandbox, repo, cancel_token=cancel_token
            )
            timings["pip_ms"] = pip_ms
            timings["build_result"] = build_result
            context._build_result = build_result
            if pip_exec is not None and pip_exec.exit_code != 0:
                if pip_exec.stderr == "cancelled by user":
                    timings["user_cancel"] = True
                    return verification_result_for_user_cancel(), timings
                result = verification_result_for_pip_failure(
                    build_result,
                    pip_exec,
                    timeout_s=BUILD_TIMEOUT_S,
                )
                timings["pytest_ms"] = 0
                return result, timings
        else:
            sandbox = Sandbox(id=sandbox_id, profile="python")
            timings["build_result"] = getattr(context, "_build_result", "reused")

        aborted = _abort_if_cancelled()
        if aborted is not None:
            return aborted

        runner = PythonTestRunner(mgr)
        t0 = time.time()
        result = runner.run(sandbox, test_path, cancel_token=cancel_token)
        timings["pytest_ms"] = int((time.time() - t0) * 1000)
        if cancel_token is not None and cancel_token.is_cancelled:
            timings["user_cancel"] = True
            return verification_result_for_user_cancel(), timings
        return result, timings
    finally:
        if sandbox is not None and mgr is not None and created_here:
            try:
                mgr.destroy(sandbox)
            except Exception:
                pass
            context._sandbox_id = None
            context._sandbox_mgr = None


def ensure_sandbox(context, repo_path: str) -> dict:
    """确保容器已创建并完成构建（幂等）。"""
    sandbox_id = getattr(context, "_sandbox_id", None)

    if sandbox_id is None:
        mgr = SandboxManager()
        try:
            sandbox = mgr.create(repo_path)
        except SandboxArchiveError as exc:
            return {"status": "error", "build_result": str(exc), "tar_error_code": exc.code}
        context._sandbox_id = sandbox.id
        context._sandbox_mgr = mgr
        context._sandbox_repo = repo_path

        build_result, _pip_ms, _pip_exec = maybe_pip_install(mgr, sandbox, repo_path)
        context._build_result = build_result
        return {"status": "created", "build_result": build_result}

    return {"status": "reused", "build_result": getattr(context, "_build_result", "")}


def verification_result_for_tar_error(exc: SandboxArchiveError) -> VerificationResult:
    return VerificationResult(all_passed=False, failure_logs=[str(exc)])


def timings_for_tar_error(exc: SandboxArchiveError) -> dict:
    return {
        "tar_error_code": exc.code,
        "tar_bytes": exc.total_bytes,
        "tar_max_bytes": exc.max_bytes,
        "tar_file_count": exc.file_count,
    }


def maybe_pip_install(
    mgr: SandboxManager,
    sandbox: Sandbox,
    repo_path: str,
    cancel_token=None,
) -> tuple[str, int, ExecResult | None]:
    """仅在有声明依赖时 pip install -e /code。"""
    repo = Path(repo_path)
    needs_install = False
    for cfg in ("pyproject.toml", "setup.py", "setup.cfg"):
        if (repo / cfg).exists():
            txt = (repo / cfg).read_text(encoding="utf-8", errors="ignore")
            if "install_requires" in txt or "dependencies" in txt:
                needs_install = True
            break

    if not needs_install:
        return "skipped (no project dependencies detected)", 0, None

    t0 = time.time()
    result = mgr.execute(
        sandbox,
        sandbox_pip_install_command(),
        timeout=BUILD_TIMEOUT_S,
        cancel_token=cancel_token,
    )
    pip_ms = int((time.time() - t0) * 1000)
    build_result = f"pip install: exit_code={result.exit_code}\n{result.stdout}"
    return build_result, pip_ms, result
