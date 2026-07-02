"""Git Tool：git blame + git diff。

无 git 仓库时优雅降级返回提示。
"""

import json
import subprocess
from dataclasses import dataclass


@dataclass
class GitBlameArgs:
    file: str  # 必填
    line: int  # 必填


@dataclass
class GitDiffArgs:
    commit_a: str = "HEAD~1"
    commit_b: str = "HEAD"
    path: str = ""  # 可选：限定路径


def git_blame(context, args: dict) -> str:
    """对指定文件的指定行执行 git blame。

    Args:
        context: ToolContext 实例。
        args: 包含 'file' 和 'line' 的字典。
    """
    file_path = args.get("file", "")
    line = args.get("line", 0)
    if not file_path:
        return "Error: 缺少必填参数 file"

    try:
        result = subprocess.run(
            ["git", "blame", "-L", f"{line},{line}", "--", file_path],
            cwd=context.root,
            capture_output=True,
            text=True,
            timeout=10,
        )
        if result.returncode != 0:
            if "not a git repository" in result.stderr.lower():
                return "Error: 不是 git 仓库"
            return f"Error: {result.stderr.strip()}"
        return _parse_blame(result.stdout.strip())
    except FileNotFoundError:
        return "Error: git 未安装"
    except subprocess.TimeoutExpired:
        return "Error: git blame 超时"


def _parse_blame(line: str) -> str:
    """解析 git blame 输出。"""
    import re

    match = re.search(
        r"([0-9a-f]+)\s+\(([^)]+)\s+(\d{4}-\d{2}-\d{2})", line
    )
    if match:
        return json.dumps({
            "commit_hash": match.group(1),
            "author": match.group(2).strip(),
            "timestamp": match.group(3),
        }, ensure_ascii=False)
    return json.dumps({"raw": line}, ensure_ascii=False)


def git_diff(context, args: dict) -> str:
    """执行 git diff。

    Args:
        context: ToolContext 实例。
        args: 包含 'commit_a'/'commit_b'/'path' 的字典。
    """
    commit_a = args.get("commit_a", "HEAD~1")
    commit_b = args.get("commit_b", "HEAD")
    file_path = args.get("path", "")

    cmd = ["git", "diff", f"{commit_a}..{commit_b}"]
    if file_path:
        cmd.extend(["--", file_path])

    try:
        result = subprocess.run(
            cmd,
            cwd=context.root,
            capture_output=True,
            text=True,
            timeout=10,
        )
        if result.returncode != 0:
            return f"Error: {result.stderr.strip()}"
        output = result.stdout.strip()
        return output if output else "(无变更)"
    except FileNotFoundError:
        return "Error: git 未安装"
    except subprocess.TimeoutExpired:
        return "Error: git diff 超时"
