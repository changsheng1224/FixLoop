"""Sandbox Tool：容器内构建 + 测试（仅 Verifier 可调用）。

每个 Tool 调用创建独立容器，执行完即销毁。
"""

import json
from dataclasses import dataclass


@dataclass
class SandboxBuildArgs:
    repo_path: str  # 必填


@dataclass
class SandboxTestArgs:
    repo_path: str  # 必填
    test_path: str = ""


def sandbox_build(context, args: dict) -> str:
    """在 Docker 容器内执行 pip install -e /code。

    Args:
        context: ToolContext（用于获取 root）。
        args: 包含 'repo_path' 的字典。
    """
    repo = args.get("repo_path", "")
    if not repo:
        return "Error: 缺少必填参数 repo_path"

    from src.harness.sandbox_manager import SandboxManager

    mgr = SandboxManager()
    try:
        sandbox = mgr.create(repo)
        result = mgr.execute(sandbox, "/entrypoint.sh build pip install -e /code", timeout=300)
        mgr.destroy(sandbox)
        return f"exit_code: {result.exit_code}\n{result.stdout}"
    except Exception as e:
        return f"Error: Docker 沙箱构建失败: {e}"


def sandbox_test(context, args: dict) -> str:
    """在 Docker 容器内运行 pytest。

    Args:
        context: ToolContext。
        args: 包含 'repo_path' 和可选 'test_path' 的字典。
    """
    repo = args.get("repo_path", "")
    test_path = args.get("test_path", "")
    if not repo:
        return "Error: 缺少必填参数 repo_path"

    from src.harness.python_runner import PythonTestRunner
    from src.harness.sandbox_manager import SandboxManager

    mgr = SandboxManager()
    try:
        sandbox = mgr.create(repo)
        runner = PythonTestRunner(mgr)
        result = runner.run(sandbox, test_path)
        mgr.destroy(sandbox)
        return json.dumps(result.to_dict(), ensure_ascii=False, indent=2)
    except Exception as e:
        return f"Error: Docker 沙箱测试失败: {e}"
