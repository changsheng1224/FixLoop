"""Sandbox Tool：容器内构建 + 测试（仅 Verifier 可调用）。

sandbox_verify 在同一容器内完成 create → build → test → destroy，
Orchestrator 可直连 harness，避免 Verifier LLM 多轮 tool 调用开销。
"""

import json
from dataclasses import dataclass

from src.state import VerificationResult


@dataclass
class SandboxBuildArgs:
    repo_path: str


@dataclass
class SandboxTestArgs:
    repo_path: str
    test_path: str = ""


def sandbox_build(context, args: dict) -> str:
    """在 Docker 容器内执行 pip install -e /code，缓存容器 ID 供后续 test 复用。"""
    return _ensure_sandbox(context, args.get("repo_path", ""))["build_result"]


def sandbox_test(context, args: dict) -> str:
    """在同一容器内运行 pytest，完成后销毁容器。"""
    repo = args.get("repo_path", "")
    test_path = args.get("test_path", "")
    if not repo:
        return "Error: 缺少必填参数 repo_path"

    result, _timings = _run_test_in_sandbox(context, repo, test_path)
    return json.dumps(result.to_dict(), ensure_ascii=False, indent=2)


def sandbox_verify(context, args: dict) -> str:
    """单容器完成 build + test，返回 VerificationResult JSON。"""
    repo = args.get("repo_path", "")
    test_path = args.get("test_path", "")
    if not repo:
        return "Error: 缺少必填参数 repo_path"

    result, timings = _run_test_in_sandbox(context, repo, test_path)
    payload = result.to_dict()
    payload["sandbox_timings"] = timings
    return json.dumps(payload, ensure_ascii=False, indent=2)


def build_sandbox_tool_registry(context) -> dict:
    """Docker sandbox 三件套工具注册表（Verifier / Baseline 共用）。"""
    return {
        "sandbox_build": {
            "schema": {"repo_path": "str"},
            "risky": False,
            "description": "在 Docker 容器内执行 pip install。参数: repo_path",
            "run": lambda args: sandbox_build(context, args),
        },
        "sandbox_test": {
            "schema": {"repo_path": "str", "test_path": "str="},
            "risky": False,
            "description": "在 Docker 容器内运行 pytest。参数: repo_path, test_path",
            "run": lambda args: sandbox_test(context, args),
        },
        "sandbox_verify": {
            "schema": {"repo_path": "str", "test_path": "str="},
            "risky": False,
            "description": "单容器 build+test。参数: repo_path, test_path",
            "run": lambda args: sandbox_verify(context, args),
        },
    }


def run_sandbox_verification(
    repo_path: str,
    test_path: str = "",
    context=None,
) -> tuple[VerificationResult, dict]:
    """Orchestrator 直连入口：不经过 Verifier LLM。"""
    if context is None:
        from agent_runtime.tool_context import ToolContext

        context = ToolContext(root=repo_path)

    result, timings = _run_test_in_sandbox(context, repo_path, test_path)
    return result, timings


def _run_test_in_sandbox(context, repo: str, test_path: str) -> tuple[VerificationResult, dict]:
    """创建容器 → 可选 pip install → pytest → 销毁。"""
    from src.harness.python_runner import PythonTestRunner
    from src.harness.sandbox_manager import Sandbox, SandboxManager

    sandbox_id = getattr(context, "_sandbox_id", None)
    mgr = getattr(context, "_sandbox_mgr", None)
    timings: dict[str, int | str] = {}
    sandbox = None
    created_here = False

    try:
        if sandbox_id is None or mgr is None:
            mgr = SandboxManager()
            sandbox = mgr.create(repo)
            created_here = True
            context._sandbox_id = sandbox.id
            context._sandbox_mgr = mgr
            context._sandbox_repo = repo
            if sandbox.timings:
                timings.update(sandbox.timings)
            build_result, pip_ms = _maybe_pip_install(mgr, sandbox, repo)
            timings["pip_ms"] = pip_ms
            timings["build_result"] = build_result
            context._build_result = build_result
        else:
            sandbox = Sandbox(id=sandbox_id, profile="python")
            timings["build_result"] = getattr(context, "_build_result", "reused")

        runner = PythonTestRunner(mgr)
        import time

        t0 = time.time()
        result = runner.run(sandbox, test_path)
        timings["pytest_ms"] = int((time.time() - t0) * 1000)
        return result, timings
    finally:
        if sandbox is not None and mgr is not None and created_here:
            try:
                mgr.destroy(sandbox)
            except Exception:
                pass
            context._sandbox_id = None
            context._sandbox_mgr = None


def _ensure_sandbox(context, repo_path: str) -> dict:
    """确保容器已创建并完成构建（幂等）。"""
    sandbox_id = getattr(context, "_sandbox_id", None)

    if sandbox_id is None:
        from src.harness.sandbox_manager import SandboxManager

        mgr = SandboxManager()
        sandbox = mgr.create(repo_path)
        context._sandbox_id = sandbox.id
        context._sandbox_mgr = mgr
        context._sandbox_repo = repo_path

        build_result, _pip_ms = _maybe_pip_install(mgr, sandbox, repo_path)
        context._build_result = build_result
        return {"status": "created", "build_result": build_result}

    return {"status": "reused", "build_result": getattr(context, "_build_result", "")}


def _maybe_pip_install(mgr, sandbox, repo_path: str) -> tuple[str, int]:
    """仅在有声明依赖时 pip install -e /code。"""
    import time
    from pathlib import Path

    repo = Path(repo_path)
    needs_install = False
    for cfg in ("pyproject.toml", "setup.py", "setup.cfg"):
        if (repo / cfg).exists():
            txt = (repo / cfg).read_text(encoding="utf-8", errors="ignore")
            if "install_requires" in txt or "dependencies" in txt:
                needs_install = True
            break

    if not needs_install:
        return "skipped (no project dependencies detected)", 0

    from src.harness.sandbox_manager import BUILD_TIMEOUT_S

    t0 = time.time()
    result = mgr.execute(
        sandbox,
        "/entrypoint.sh build pip install -e /code 2>&1 | tail -5",
        timeout=BUILD_TIMEOUT_S,
    )
    pip_ms = int((time.time() - t0) * 1000)
    build_result = f"pip install: exit_code={result.exit_code}\n{result.stdout}"
    return build_result, pip_ms
