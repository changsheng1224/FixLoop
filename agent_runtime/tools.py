"""工具定义：参数 dataclass + 执行函数 + 工具注册。

每个工具由两部分组成：
1. 参数 dataclass — 定义工具接受的参数及其类型和默认值
2. 执行函数 — 接受 ToolContext + dict，执行工具逻辑，返回结果字符串

auto_schema() 从 dataclass 自动推导参数字典，新增工具无需手写 schema。
"""

import os
import subprocess
from dataclasses import dataclass
from pathlib import Path

from agent_runtime.schema_utils import auto_schema

# ============================================================================
# 工具执行层级常量
# ============================================================================

TIER_HOST = "host"
TIER_CONTAINER = "container"

# ============================================================================
# 工具参数 Dataclass
# ============================================================================


@dataclass
class ListFilesArgs:
    """列出目录内容。"""

    path: str = "."
    glob: str = ""
    depth: int = 1
    max_results: int = 200


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
    context_lines: int = 0  # 匹配行前后各多显示 N 行


@dataclass
class GrepArgs:
    """内容搜索（rg 优先，Python re fallback）。"""

    pattern: str  # 必填
    path: str = "."
    glob: str = ""  # 如 *.py
    ignore_case: bool = False
    context_lines: int = 0
    max_results: int = 50


@dataclass
class WriteFileArgs:
    """创建或覆盖文件。"""

    path: str = ""
    content: str = ""
    append: bool = False  # True 时追加而非覆盖


@dataclass
class PatchFileArgs:
    """精确文本替换或 unified diff 多 hunk 修补。"""

    path: str = ""
    old_text: str = ""
    new_text: str = ""
    diff: str = ""


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

    遍历目录，输出 [F] 文件 / [D] 目录。depth=1 仅直接子项；更大 depth 递归列文件路径。
    glob 过滤（如 ``*.py``）；depth=0 表示不限层数，受 max_results 限制。
    """
    raw_path = args.get("path", ".")
    glob_pattern = args.get("glob", "") or ""
    try:
        depth = int(args.get("depth", 1))
    except (TypeError, ValueError):
        depth = 1
    try:
        max_results = int(args.get("max_results", 200))
    except (TypeError, ValueError):
        max_results = 200

    try:
        target = context.resolve(raw_path)
    except ValueError as e:
        return f"Error: {e}"

    if not target.exists():
        return f"Error: 目录不存在: {raw_path}"
    if not target.is_dir():
        return f"Error: 不是目录: {raw_path}"

    from agent_runtime.file_listing import list_directory_entries

    lines, total = list_directory_entries(
        target,
        depth=depth,
        glob_pattern=glob_pattern,
        max_results=max_results,
        ignored_names=IGNORED_PATH_NAMES,
    )

    if not lines:
        if glob_pattern:
            return f"(无匹配) {raw_path} glob={glob_pattern!r}"
        return f"(空目录) {raw_path}"

    if total > len(lines):
        lines.append(f"(另有 {total - len(lines)} 项未显示，可缩小 glob/depth 或提高 max_results)")
    return "\n".join(lines)


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

    from agent_runtime.io_limits import is_likely_binary, read_max_bytes
    from agent_runtime.sensitive_paths import is_sensitive_path, sensitive_reject_message

    if is_sensitive_path(raw_path) or is_sensitive_path(target):
        return sensitive_reject_message(raw_path)
    try:
        size = target.stat().st_size
    except OSError as e:
        return f"Error: 无法读取文件: {e}"
    limit = read_max_bytes()
    if size > limit:
        return f"Error: 文件过大 ({size} bytes > {limit})，拒绝读取: {raw_path}"
    if is_likely_binary(target):
        return f"Error: 疑似二进制文件，拒绝读取: {raw_path}"

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
    """代码搜索（已委托 grep，保留兼容名。新调用请直接用 grep）。"""
    return tool_grep(context, args)


def tool_grep(context, args: dict) -> str:
    """内容搜索：rg 优先，不可用时 Python re + rglob fallback。

    Args 必须包含 'pattern'，可选 'path'、'glob'、'ignore_case'、'context_lines'、'max_results'。
    输出格式: path:line:text，超 max_results 附截断提示。
    """
    pattern = args.get("pattern", "")
    if not pattern:
        return "Error: 缺少必填参数 pattern"
    raw_path = args.get("path", ".")
    glob_filter = args.get("glob", "") or ""
    ignore_case = bool(args.get("ignore_case", False))
    ctx = int(args.get("context_lines", 0) or 0)
    max_results = int(args.get("max_results", 50) or 50)

    try:
        target = context.resolve(raw_path)
    except ValueError as e:
        return f"Error: {e}"

    from agent_runtime.sensitive_paths import is_sensitive_path, sensitive_reject_message

    if is_sensitive_path(raw_path) or is_sensitive_path(target):
        return sensitive_reject_message(raw_path)

    if not target.exists():
        return f"Error: 路径不存在: {raw_path}"

    # rg 优先
    result, total = _grep_rg(pattern, target, glob_filter, ignore_case, ctx, max_results)
    if result is not None:
        return _format_grep_result(result, total, max_results)

    # Fallback: Python re + rglob
    result, total = _grep_python(pattern, target, glob_filter, ignore_case, ctx, max_results)
    return _format_grep_result(result, total, max_results)


def _grep_rg(
    pattern: str,
    target: Path,
    glob_filter: str,
    ignore_case: bool,
    context_lines: int,
    max_results: int,
) -> tuple[list[str] | None, int]:
    """rg 搜索，失败返回 None。"""
    try:
        cmd = ["rg", "-n", "--no-heading"]
        if ignore_case:
            cmd.append("-i")
        if context_lines > 0:
            cmd.extend(["-C", str(context_lines)])
        if glob_filter:
            cmd.extend(["-g", glob_filter])
        cmd.extend([pattern, str(target)])
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
        if result.returncode == 0:
            lines = [line for line in result.stdout.splitlines() if line.strip()]
            return lines, len(lines)
        elif result.returncode == 1:
            return [], 0
        return None, 0
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return None, 0


def _grep_python(
    pattern: str,
    target: Path,
    glob_filter: str,
    ignore_case: bool,
    context_lines: int,
    max_results: int,
) -> tuple[list[str], int]:
    """Python re + rglob fallback 搜索。"""
    import re

    flags = re.IGNORECASE if ignore_case else 0
    try:
        regex = re.compile(pattern, flags)
    except re.error:
        # 非正则字面量 → escape
        regex = re.compile(re.escape(pattern), flags)

    matches: list[str] = []
    total = 0
    target.rglob if glob_filter else lambda: target.rglob("*")

    for filepath in target.rglob(glob_filter) if glob_filter else target.rglob("*"):
        if filepath.is_dir():
            continue
        if any(ign in filepath.parts for ign in IGNORED_PATH_NAMES):
            continue
        from agent_runtime.sensitive_paths import is_sensitive_path

        if is_sensitive_path(filepath):
            continue
        if filepath.suffix not in (
            ".py",
            ".txt",
            ".md",
            ".toml",
            ".yaml",
            ".yml",
            ".cfg",
            ".ini",
            ".json",
            ".sh",
        ):
            continue
        try:
            text = filepath.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        lines_list = text.splitlines()
        for i, line in enumerate(lines_list, 1):
            if regex.search(line):
                rel = filepath.relative_to(target)
                if context_lines > 0:
                    ctx_start = max(1, i - context_lines)
                    ctx_end = min(len(lines_list), i + context_lines)
                    for j in range(ctx_start, ctx_end + 1):
                        prefix = ">" if j == i else " "
                        matches.append(f"{rel}:{j}:{prefix} {lines_list[j - 1].strip()[:200]}")
                        total += 1
                else:
                    matches.append(f"{rel}:{i}: {line.strip()[:200]}")
                    total += 1
                if len(matches) >= max_results * 2:  # 含上下文会有更多行
                    break
        if len(matches) >= max_results * 2:
            break

    return matches[:max_results], total


def _merge_adjacent_lines(lines: list[str]) -> list[str]:
    """合并同文件连续行号为范围（file:start-end:），去重降噪。"""
    import re

    if not lines:
        return []
    merged: list[str] = []
    # 解析为 (file, line, text) 三元组
    parsed: list[tuple[str, int, str]] = []
    for ln in lines:
        m = re.match(r"^(.+?):(\d+):(.+)$", ln)
        if m:
            parsed.append((m.group(1), int(m.group(2)), m.group(3).strip()))
        else:
            parsed.append((ln, -1, ""))

    i = 0
    while i < len(parsed):
        fname, lnum, text = parsed[i]
        if lnum < 0:
            merged.append(fname)
            i += 1
            continue
        # 找连续同文件行
        j = i + 1
        while (
            j < len(parsed)
            and parsed[j][1] >= 0
            and parsed[j][0] == fname
            and parsed[j][1] == parsed[j - 1][1] + 1
        ):
            j += 1
        if j - i >= 3:
            # 3 行及以上合并为范围
            merged.append(f"{fname}:{parsed[i][1]}-{parsed[j - 1][1]}:")
            for k in range(i, j):
                merged.append(f"  {parsed[k][1]}: {parsed[k][2]}")
        else:
            for k in range(i, j):
                merged.append(f"{parsed[k][0]}:{parsed[k][1]}: {parsed[k][2]}")
        i = j

    return merged


def _format_grep_result(lines: list[str], total: int, max_results: int) -> str:
    if not lines:
        return "(无匹配)"
    merged = _merge_adjacent_lines(lines)
    result = "\n".join(merged)
    if total > len(lines):
        result += (
            f"\n... 另有 {total - len(lines)} 条匹配未显示（可缩小 path/glob 或提高 max_results）"
        )
    from agent_runtime.io_limits import grep_max_bytes, truncate_text

    result, truncated = truncate_text(result, grep_max_bytes(), label="grep")
    if truncated:
        result += "\n[oversized_grep]"
    return result


# ============================================================================
# 高风险写工具执行函数
# ============================================================================


def tool_write_file(context, args: dict) -> str:
    """创建或覆盖文件，自动创建父目录。

    Args 必须包含 'path' 和 'content'，可选 'append'（默认 False）。
    """
    raw_path = args.get("path", "")
    if not raw_path:
        return "Error: 缺少必填参数 path"
    content = args.get("content", "")
    append = args.get("append", False)

    from agent_runtime.sensitive_paths import is_sensitive_path, sensitive_reject_message

    if is_sensitive_path(raw_path):
        return sensitive_reject_message(raw_path)

    try:
        target = context.resolve(raw_path)
    except ValueError as e:
        return f"Error: {e}"

    if is_sensitive_path(target):
        return sensitive_reject_message(raw_path)

    from agent_runtime.atomic_io import atomic_write_text

    try:
        if append and target.exists():
            payload = target.read_text(encoding="utf-8") + content
            mode = "已追加到"
        else:
            payload = content
            mode = "已写入"
        atomic_write_text(target, payload)
    except OSError as e:
        return f"Error: 写入文件失败: {e}"

    return f"{mode} {raw_path}（{len(content)} 字符）"


def tool_patch_file(context, args: dict) -> str:
    """精确文本替换或 unified diff 多 hunk 修补。

    Args 必须包含 path，以及 diff 或 (old_text + new_text)。
    old_text 必须出现恰好 1 次；diff 支持多个 @@ hunk。
    """
    raw_path = args.get("path", "")
    if not raw_path:
        return "Error: 缺少必填参数 path"

    from agent_runtime.sensitive_paths import is_sensitive_path, sensitive_reject_message

    if is_sensitive_path(raw_path):
        return sensitive_reject_message(raw_path)

    try:
        target = context.resolve(raw_path)
    except ValueError as e:
        return f"Error: {e}"

    if is_sensitive_path(target):
        return sensitive_reject_message(raw_path)

    if not target.exists():
        return f"Error: 文件不存在: {raw_path}"
    if not target.is_file():
        return f"Error: 不是文件: {raw_path}"

    try:
        text = target.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        return f"Error: 无法以 UTF-8 编码读取: {raw_path}"

    from agent_runtime.atomic_io import atomic_write_text
    from agent_runtime.patch_engine import apply_plan, build_preview, parse_patch_input

    try:
        plan = parse_patch_input(args)
    except ValueError as e:
        return f"Error: {e}"

    if plan.mode == "legacy":
        count = text.count(plan.old_text)
        if count == 0:
            return "Error: old_text 在文件中未找到（出现 0 次）。old_text 必须恰好出现 1 次。"
        if count > 1:
            return f"Error: old_text 出现 {count} 次，必须恰好出现 1 次。请提供更多上下文使其唯一。"

    new_text = apply_plan(text, plan)
    if new_text is None:
        return "Error: 补丁无法应用到文件（hunk 与文件内容不匹配）。"

    try:
        atomic_write_text(target, new_text)
    except OSError as e:
        return f"Error: 写入文件失败: {e}"

    preview = build_preview(raw_path, plan)
    delta = preview.lines_added - preview.lines_removed
    if preview.hunk_count == 1 and plan.mode == "legacy":
        return f"已修补 {raw_path}（替换 1 处，{delta:+d} 字符）"
    return (
        f"已修补 {raw_path}（{preview.hunk_count} 个 hunk，"
        f"-{preview.lines_removed}/+{preview.lines_added} 行）"
    )


def tool_run_shell(context, args: dict) -> str:
    """在 workspace 根目录执行 Shell 命令。

    Args 必须包含 'command'，可选 'timeout'(默认20s)。
    环境变量经过白名单过滤；输出经 redact_text 脱敏。
    """
    from agent_runtime.security import check_shell_command, redact_text
    from agent_runtime.security import shell_env as _shell_env

    command = args.get("command", "")
    if not command:
        return "Error: 缺少必填参数 command"

    allowed, reason = check_shell_command(command)
    if not allowed:
        return f"Error: Shell 命令被安全策略拒绝 ({reason}): {command[:100]}"
    try:
        timeout = int(args.get("timeout", 20))
    except (ValueError, TypeError):
        timeout = 20
    timeout = max(1, min(timeout, 120))  # 限制 1-120 秒

    root = context.root
    provider = getattr(context, "shell_env_provider", None)
    if callable(provider):
        env = provider()
    else:
        env = _shell_env(root=root)

    cancel_token = getattr(context, "cancel_token", None)
    if cancel_token is not None:
        return redact_text(_run_shell_cancellable(command, root, env, timeout, cancel_token))
    return redact_text(_run_shell_blocking(command, root, env, timeout))


def _run_shell_blocking(command: str, root, env, timeout: int) -> str:
    try:
        result = subprocess.run(
            command,
            cwd=root,
            shell=True,
            capture_output=True,
            text=True,
            timeout=timeout,
            env=env,
        )
    except subprocess.TimeoutExpired:
        return f"Error: 命令超时（{timeout} 秒）: {command[:100]}"

    return _format_shell_result(result.returncode, result.stdout, result.stderr)


def _kill_process_tree(proc: subprocess.Popen) -> None:
    """终止 shell 子进程（含 Windows 下 shell 派生的孙进程）。"""
    if proc.poll() is not None:
        return
    killed = False
    if os.name == "nt":
        result = subprocess.run(
            ["taskkill", "/F", "/T", "/PID", str(proc.pid)],
            capture_output=True,
            check=False,
        )
        killed = result.returncode == 0
    else:
        proc.kill()
        killed = True
    if not killed and proc.poll() is None:
        proc.terminate()
    try:
        proc.wait(timeout=0.5)
    except subprocess.TimeoutExpired:
        proc.kill()


def _run_shell_cancellable(command: str, root, env, timeout: int, cancel_token) -> str:
    import shlex
    import time

    popen_kwargs = {}
    popen_command = command
    use_shell = True
    if os.name == "nt":
        cmd_name = command.strip().split()[0].split("/")[-1].split("\\")[-1].lower()
        if cmd_name not in {"echo", "dir", "set"}:
            try:
                popen_command = shlex.split(command)
                use_shell = False
            except ValueError:
                popen_command = command
                use_shell = True
        popen_kwargs["creationflags"] = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
    else:
        popen_kwargs["start_new_session"] = True

    proc = subprocess.Popen(
        popen_command,
        cwd=root,
        shell=use_shell,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=env,
        **popen_kwargs,
    )
    deadline = time.time() + timeout
    poll_s = 0.05
    while proc.poll() is None:
        if cancel_token.is_cancelled:
            _kill_process_tree(proc)
            return f"Error: 命令已取消: {command[:100]}"
        if time.time() >= deadline:
            _kill_process_tree(proc)
            return f"Error: 命令超时（{timeout} 秒）: {command[:100]}"
        time.sleep(poll_s)

    stdout, stderr = proc.communicate(timeout=1)
    return _format_shell_result(proc.returncode or 0, stdout or "", stderr or "")


def _format_shell_result(returncode: int, stdout: str, stderr: str) -> str:
    from agent_runtime.io_limits import shell_max_bytes, truncate_text

    stdout, _ = truncate_text(stdout, shell_max_bytes(), label="stdout")
    stderr, _ = truncate_text(stderr, shell_max_bytes(), label="stderr")
    out = []
    out.append(f"exit_code: {returncode}")
    if stdout.strip():
        out.append(f"stdout:\n{stdout.rstrip()}")
    if stderr.strip():
        out.append(f"stderr:\n{stderr.rstrip()}")
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
    from agent_runtime.security import shell_env

    context.shell_env_provider = lambda: shell_env(root=context.root)

    registry = {}

    # ---- list_files ----
    registry["list_files"] = {
        "schema": auto_schema(ListFilesArgs),
        "risky": False,
        "execution_tier": TIER_HOST,
        "description": "列出目录内容。参数: path（默认 '.'）",
        "run": lambda args: tool_list_files(context, args),
    }

    # ---- read_file ----
    registry["read_file"] = {
        "schema": auto_schema(ReadFileArgs),
        "risky": False,
        "execution_tier": TIER_HOST,
        "description": "按行号范围读取 UTF-8 文件。参数: path, start(默认1), end(默认200)",
        "run": lambda args: tool_read_file(context, args),
    }

    # ---- grep ----
    registry["grep"] = {
        "schema": auto_schema(GrepArgs),
        "risky": False,
        "execution_tier": TIER_HOST,
        "description": (
            "内容搜索（rg 优先，Python fallback）。"
            "参数: pattern, path, glob, ignore_case, context_lines, max_results"
        ),
        "run": lambda args: tool_grep(context, args),
    }

    # ---- search ----
    registry["search"] = {
        "schema": auto_schema(SearchArgs),
        "risky": False,
        "execution_tier": TIER_HOST,
        "description": "代码搜索（rg 优先，Python fallback）。参数: pattern, path（默认 '.'）",
        "run": lambda args: tool_search(context, args),
    }

    # ---- write_file ----
    registry["write_file"] = {
        "schema": auto_schema(WriteFileArgs),
        "risky": True,
        "execution_tier": TIER_HOST,
        "description": "创建或覆盖文件，自动创建父目录。参数: path, content",
        "run": lambda args: tool_write_file(context, args),
    }

    # ---- patch_file ----
    registry["patch_file"] = {
        "schema": auto_schema(PatchFileArgs),
        "risky": True,
        "execution_tier": TIER_HOST,
        "description": ("精确文本替换：old_text 必须恰好出现 1 次。参数: path, old_text, new_text"),
        "run": lambda args: tool_patch_file(context, args),
    }

    # ---- run_shell ----
    registry["run_shell"] = {
        "schema": auto_schema(RunShellArgs),
        "risky": True,
        "execution_tier": TIER_HOST,
        "description": "执行 Shell 命令。参数: command, timeout(默认20s，最大120s)",
        "run": lambda args: tool_run_shell(context, args),
    }

    return registry


def legal_tool_names(registry: dict) -> set[str]:
    """返回注册表中所有可调用工具名的集合。"""
    return set(registry.keys())
