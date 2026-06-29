"""工具定义：参数 dataclass + 执行函数 + 工具注册。

每个工具由两部分组成：
1. 参数 dataclass — 定义工具接受的参数及其类型和默认值
2. 执行函数 — 接受 ToolContext + dict，执行工具逻辑，返回结果字符串

auto_schema() 从 dataclass 自动推导参数字典，新增工具无需手写 schema。
"""

from dataclasses import dataclass

# ============================================================================
# 工具参数 Dataclass
# ============================================================================


@dataclass
class ListFilesArgs:
    """列出目录内容。"""

    path: str = "."


@dataclass
class ReadFileArgs:
    """按行号范围读取 UTF-8 文件。"""

    path: str  # 必填
    start: int = 1
    end: int = 200


@dataclass
class SearchArgs:
    """代码搜索（rg 优先，Python fallback）。"""

    pattern: str  # 必填
    path: str = "."


@dataclass
class WriteFileArgs:
    """创建或覆盖文件（M2 实现）。"""

    path: str = ""
    content: str = ""


@dataclass
class PatchFileArgs:
    """精确文本替换（M2 实现）。"""

    path: str = ""
    old_text: str = ""
    new_text: str = ""


@dataclass
class RunShellArgs:
    """执行 Shell 命令（M2 实现）。"""

    command: str = ""
    timeout: int = 20


# ============================================================================
# 忽略的路径名（list_files + search 都会跳过）
# ============================================================================

IGNORED_PATH_NAMES = {
    "__pycache__",
    ".git",
    ".agent",
    ".pytest_cache",
    ".ruff_cache",
    "node_modules",
    ".venv",
    "venv",
    ".idea",
    ".vscode",
    "dist",
    "build",
    ".eggs",
    "*.egg-info",
}
