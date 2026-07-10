"""Sandbox Tool：容器内构建 + 测试（仅 Verifier 可调用）。

sandbox_verify 在同一容器内完成 create → build → test → destroy，
Orchestrator 可直连 harness，避免 Verifier LLM 多轮 tool 调用开销。
"""

import json
from dataclasses import dataclass

from src.harness.sandbox_verify import ensure_sandbox, run_sandbox_verification_flow
from src.state import VerificationResult

# 兼容旧测试 / 内部引用
_ensure_sandbox = ensure_sandbox
_run_test_in_sandbox = run_sandbox_verification_flow


@dataclass
class SandboxBuildArgs:
    repo_path: str


@dataclass
class SandboxTestArgs:
    repo_path: str
    test_path: str = ""


def sandbox_build(context, args: dict) -> str:
    """在 Docker 容器内执行 pip install -e /code，缓存容器 ID 供后续 test 复用。"""
    return ensure_sandbox(context, args.get("repo_path", ""))["build_result"]


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

    return _run_test_in_sandbox(context, repo_path, test_path)
