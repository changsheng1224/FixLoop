"""工具定义：参数 dataclass + 执行函数 + 工具注册。

每个工具由两部分组成：
1. 参数 dataclass — 定义工具接受的参数及其类型和默认值
2. 执行函数 — 接受 ToolContext + dict，执行工具逻辑，返回结果字符串

auto_schema() 从 dataclass 自动推导参数字典，新增工具无需手写 schema。
"""

import subprocess
from dataclasses import dataclass
from pathlib import Path

from agent_runtime.schema_utils import auto_schema

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


# ============================================================================
# 只读工具执行函数
# ============================================================================


def tool_list_files(context, args: dict) -> str:
    """列出目录内容。

    遍历目录，输出 [F] 文件 / [D] 目录，过滤忽略的路径名。
    """

    raw_path = args.get("path", ".")
    try:
        target = context.resolve(raw_path)
    except ValueError as e:
        return f"Error: {e}"

    if not target.exists():
        return f"Error: 目录不存在: {raw_path}"
    if not target.is_dir():
        return f"Error: 不是目录: {raw_path}"

    items = []
    for child in sorted(target.iterdir()):
        name = child.name
        # 过滤忽略的路径名
        if name in IGNORED_PATH_NAMES or name.startswith("."):
            continue
        prefix = "[D]" if child.is_dir() else "[F]"
        items.append(f"{prefix} {name}")

    if not items:
        return f"(空目录) {raw_path}"

    return "\n".join(items)


def tool_read_file(context, args: dict) -> str:
    """按行号范围读取文件，输出带行号前缀。

    Args 必须包含 'path'，可选 'start'(默认1) 和 'end'(默认200)。
    """
    raw_path = args.get("path", "")
    if not raw_path:
        return "Error: 缺少必填参数 path"
    start = int(args.get("start", 1))
    end = int(args.get("end", 200))

    try:
        target = context.resolve(raw_path)
    except ValueError as e:
        return f"Error: {e}"

    if not target.exists():
        return f"Error: 文件不存在: {raw_path}"
    if not target.is_file():
        return f"Error: 不是文件: {raw_path}"

    try:
        lines = target.read_text(encoding="utf-8").splitlines()
    except UnicodeDecodeError:
        return f"Error: 无法以 UTF-8 编码读取: {raw_path}"

    total = len(lines)
    start = max(1, start)
    end = min(end, total)

    if start > total:
        return f"Error: start({start}) 超出文件行数({total})"

    output = []
    for i in range(start - 1, end):
        output.append(f"{i + 1:4d} | {lines[i]}")

    header = f"# {raw_path}  ({start}-{end}/{total} 行)\n"
    return header + "\n".join(output)


def tool_search(context, args: dict) -> str:
    """代码搜索：优先使用 ripgrep，不可用时 fallback 到纯 Python。

    Args 必须包含 'pattern'，可选 'path'(默认".")。
    """
    pattern = args.get("pattern", "")
    if not pattern:
        return "Error: 缺少必填参数 pattern"
    raw_path = args.get("path", ".")

    try:
        target = context.resolve(raw_path)
    except ValueError as e:
        return f"Error: {e}"

    if not target.exists():
        return f"Error: 路径不存在: {raw_path}"

    # 优先使用 ripgrep
    result = _search_rg(pattern, target)
    if result is not None:
        return result

    # Fallback: 纯 Python 搜索
    return _search_python(pattern, target)


def _search_rg(pattern: str, target: Path) -> str | None:
    """尝试用 ripgrep 搜索，失败返回 None。"""
    try:
        result = subprocess.run(
            ["rg", "-n", "--smart-case", pattern, str(target)],
            capture_output=True,
            text=True,
            timeout=30,
        )
        if result.returncode == 0:
            lines = result.stdout.strip().splitlines()[:50]  # 最多 50 条
            if not lines or not lines[0]:
                return "(无匹配)"
            return "\n".join(lines)
        elif result.returncode == 1:
            return "(无匹配)"
        return None
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return None


def _search_python(pattern: str, target: Path) -> str:
    """纯 Python 搜索：逐文件遍历匹配。"""
    pattern_lower = pattern.lower()
    matches = []
    count = 0

    for filepath in target.rglob("*"):
        if count >= 50:
            break
        # 跳过目录和忽略的路径
        if filepath.is_dir():
            continue
        if any(ign in filepath.parts for ign in IGNORED_PATH_NAMES):
            continue
        if filepath.suffix not in (".py", ".txt", ".md", ".toml", ".yaml", ".yml", ".cfg", ".ini"):
            continue

        try:
            text = filepath.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue

        for i, line in enumerate(text.splitlines(), 1):
            if pattern_lower in line.lower():
                rel = filepath.relative_to(target)
                matches.append(f"{rel}:{i}: {line.strip()[:200]}")
                count += 1
                if count >= 50:
                    break

    if not matches:
        return "(无匹配)"
    return "\n".join(matches)


# ============================================================================
# 高风险写工具执行函数
# ============================================================================


def tool_write_file(context, args: dict) -> str:
    """创建或覆盖文件，自动创建父目录。

    Args 必须包含 'path' 和 'content'。
    """
    raw_path = args.get("path", "")
    if not raw_path:
        return "Error: 缺少必填参数 path"
    content = args.get("content", "")

    try:
        target = context.resolve(raw_path)
    except ValueError as e:
        return f"Error: {e}"

    # 自动创建父目录
    target.parent.mkdir(parents=True, exist_ok=True)

    try:
        target.write_text(content, encoding="utf-8")
    except OSError as e:
        return f"Error: 写入文件失败: {e}"

    return f"已写入 {raw_path}（{len(content)} 字符）"


def tool_patch_file(context, args: dict) -> str:
    """精确文本替换：old_text 必须出现恰好 1 次。

    Args 必须包含 'path'、'old_text'、'new_text'。
    """
    raw_path = args.get("path", "")
    if not raw_path:
        return "Error: 缺少必填参数 path"
    old_text = args.get("old_text", "")
    new_text = args.get("new_text", "")

    try:
        target = context.resolve(raw_path)
    except ValueError as e:
        return f"Error: {e}"

    if not target.exists():
        return f"Error: 文件不存在: {raw_path}"
    if not target.is_file():
        return f"Error: 不是文件: {raw_path}"

    try:
        text = target.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        return f"Error: 无法以 UTF-8 编码读取: {raw_path}"

    count = text.count(old_text)
    if count == 0:
        return "Error: old_text 在文件中未找到（出现 0 次）。old_text 必须恰好出现 1 次。"
    if count > 1:
        return f"Error: old_text 出现 {count} 次，必须恰好出现 1 次。请提供更多上下文使其唯一。"

    target.write_text(text.replace(old_text, new_text, 1), encoding="utf-8")
    return f"已修补 {raw_path}（替换 1 处，{len(new_text) - len(old_text):+d} 字符）"


def tool_run_shell(context, args: dict) -> str:
    """在 workspace 根目录执行 Shell 命令。

    Args 必须包含 'command'，可选 'timeout'(默认20s)。
    环境变量经过白名单过滤，禁止泄露密钥。
    """
    from agent_runtime.security import shell_env as _shell_env

    command = args.get("command", "")
    if not command:
        return "Error: 缺少必填参数 command"
    try:
        timeout = int(args.get("timeout", 20))
    except (ValueError, TypeError):
        timeout = 20
    timeout = max(1, min(timeout, 120))  # 限制 1-120 秒

    root = context.root

    try:
        result = subprocess.run(
            command,
            cwd=root,
            shell=True,
            capture_output=True,
            text=True,
            timeout=timeout,
            env=_shell_env(root=root),
        )
    except subprocess.TimeoutExpired:
        return f"Error: 命令超时（{timeout} 秒）: {command[:100]}"

    out = []
    if result.stdout.strip():
        out.append(f"stdout:\n{result.stdout.rstrip()}")
    if result.stderr.strip():
        out.append(f"stderr:\n{result.stderr.rstrip()}")
    out.insert(0, f"exit_code: {result.returncode}")

    return "\n".join(out)


# ============================================================================
# 工具注册表
# ============================================================================


def build_tool_registry(context) -> dict:
    """构建工具注册表：工具名 → {schema, risky, description, run}。

    Args:
        context: ToolContext 实例。

    Returns:
        工具注册表字典。
    """
    registry = {}

    # ---- list_files ----
    registry["list_files"] = {
        "schema": auto_schema(ListFilesArgs),
        "risky": False,
        "description": "列出目录内容。参数: path（默认 '.'）",
        "run": lambda args: tool_list_files(context, args),
    }

    # ---- read_file ----
    registry["read_file"] = {
        "schema": auto_schema(ReadFileArgs),
        "risky": False,
        "description": "按行号范围读取 UTF-8 文件。参数: path, start(默认1), end(默认200)",
        "run": lambda args: tool_read_file(context, args),
    }

    # ---- search ----
    registry["search"] = {
        "schema": auto_schema(SearchArgs),
        "risky": False,
        "description": "代码搜索（rg 优先，Python fallback）。参数: pattern, path（默认 '.'）",
        "run": lambda args: tool_search(context, args),
    }

    # ---- write_file ----
    registry["write_file"] = {
        "schema": auto_schema(WriteFileArgs),
        "risky": True,
        "description": "创建或覆盖文件，自动创建父目录。参数: path, content",
        "run": lambda args: tool_write_file(context, args),
    }

    # ---- patch_file ----
    registry["patch_file"] = {
        "schema": auto_schema(PatchFileArgs),
        "risky": True,
        "description": (
            "精确文本替换：old_text 必须恰好出现 1 次。参数: path, old_text, new_text"
        ),
        "run": lambda args: tool_patch_file(context, args),
    }

    # ---- run_shell ----
    registry["run_shell"] = {
        "schema": auto_schema(RunShellArgs),
        "risky": True,
        "description": "执行 Shell 命令。参数: command, timeout(默认20s，最大120s)",
        "run": lambda args: tool_run_shell(context, args),
    }

    return registry


def legal_tool_names(registry: dict) -> set[str]:
    """返回注册表中所有可调用工具名的集合。"""
    return set(registry.keys())

