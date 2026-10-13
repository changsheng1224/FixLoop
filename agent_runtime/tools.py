"""工具定义：参数 dataclass + 执行函数 + 工具注册。

每个工具由两部分组成：
1. 参数 dataclass — 定义工具接受的参数及其类型和默认值
2. 执行函数 — 接受 ToolContext + dict，执行工具逻辑，返回结果字符串

auto_schema() 从 dataclass 自动推导参数字典，新增工具无需手写 schema。
"""

import os
import subprocess
import sys
from dataclasses import dataclass

from agent_runtime.schema_utils import auto_schema
from agent_runtime.tool_context import ToolContext
from agent_runtime.tool_result import ToolResult, ToolStatus

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
    end: int = 100  # Phase B：默认窗口约 100 行


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
class CodeLookupArgs:
    """Find definitions or references for an exact source position."""

    path: str
    line: int
    column: int
    operation: str = "definition"
    max_results: int = 50


@dataclass
class CodeRelationsArgs:
    """Summarize only source evidence observed in the current task."""

    max_files: int = 8
    top_k: int = 6
    token_budget: int = 1500


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
class ApplyPatchArgs:
    """Codex 风格 apply_patch（*** Begin/End Patch）。"""

    patch: str = ""


@dataclass
class QuickTestArgs:
    """环内快检。"""

    nodeid: str = ""
    path: str = ""
    timeout: int = 60


@dataclass
class RunShellArgs:
    """执行 Shell 命令（M2 实现）。"""

    command: str = ""
    timeout: int = 20


@dataclass
class ExpandObservationArgs:
    """Expand a previously referenced governed observation."""

    observation_id: str = ""
    max_tokens: int = 2000


# ============================================================================
# 忽略的路径名（list_files + grep 都会跳过）
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


def tool_list_files(context, args: dict):
    if (routed := _sandbox_workspace_result(context, "list_files", args)) is not None:
        return routed
    from agent_runtime.code_exploration.io import list_files_result

    return list_files_result(context, args)


def _code_lookup_structured(context, args: dict):
    if (routed := _sandbox_workspace_result(context, "code_lookup", args)) is not None:
        return routed
    service = _exploration_service(context)
    service.emit(
        "query_start",
        {"query_type": "code_lookup", "operation": args.get("operation", "definition")},
    )
    result = service.lookup(args)
    retrieval = result.metadata.get("retrieval_result", {})
    service.emit(
        "query_end",
        {
            "query_type": "code_lookup",
            "query_id": retrieval.get("query_id", ""),
            "hits": len(retrieval.get("hits", [])),
            "completeness": retrieval.get("completeness", "unknown"),
            "truncation_reasons": retrieval.get("truncation_reasons", []),
        },
    )
    if retrieval.get("degradation_reason") not in {None, "disabled"}:
        service.emit(
            "lsp_degraded",
            {"query_id": retrieval.get("query_id", ""), "reason": retrieval["degradation_reason"]},
        )
    return result


def _exploration_service(context):
    from agent_runtime.code_exploration.service import CodeExplorationService

    service = context.exploration_service
    if service is None:
        service = CodeExplorationService(
            context, mode=context.exploration_mode, server_argv=context.lsp_argv
        )
        context.exploration_service = service
    return service


def _code_relations_structured(context, args: dict):
    if (routed := _sandbox_workspace_result(context, "code_relations", args)) is not None:
        return routed
    service = _exploration_service(context)
    service.emit("query_start", {"query_type": "code_relations"})
    result = service.relations(args)
    view = result.metadata.get("relation_view", {})
    service.emit(
        "query_end",
        {
            "query_type": "code_relations",
            "view_revision": view.get("view_revision", 0),
            "coverage": view.get("coverage", {}),
        },
    )
    return result


def tool_read_file(context, args: dict):
    if (routed := _sandbox_workspace_result(context, "read_file", args)) is not None:
        return routed
    from agent_runtime.code_exploration.io import read_file_result

    result = read_file_result(context, args)
    if result.ok:
        _mark_edit_lock_read(context, str(args.get("path", "")))
    return result


def _resolve_edit_lock(context):
    return context.edit_lock


def _sandbox_workspace_result(context, name, args):
    from agent_runtime.linux_sandbox.workspace_routing import (
        execute_workspace_tool,
        sandbox_enabled,
    )

    return execute_workspace_tool(context, name, args) if sandbox_enabled(context) else None


def _mark_edit_lock_read(context, raw_path: str, *, result=None) -> None:
    lock = _resolve_edit_lock(context)
    if lock is not None:
        from agent_runtime.linux_sandbox.workspace_routing import sandbox_enabled

        if sandbox_enabled(context):
            if result is not None:
                lock.mark_read_evidence(raw_path, result)
        else:
            lock.mark_read(raw_path)


def _reject_if_edit_locked(context, raw_path: str, *, operation="modify") -> str | None:
    lock = _resolve_edit_lock(context)
    if lock is None:
        return None
    ok, reason = lock.check_write(raw_path, operation=operation)
    if ok:
        return None
    return f"Error: edit_lock rejected write ({reason})"


def _reject_if_write_serial(context) -> str | None:
    """每 turn 至多一次写（Phase B）；无 guard 时放行。"""
    if getattr(context, "write_serial", False):
        if getattr(context, "_write_done_this_turn", False):
            return "Error: write_serial: 本 turn 已有写操作，请先读结果再决策后再写"
        return None
    lock = _resolve_edit_lock(context)
    if lock is not None and getattr(lock, "write_serial", False):
        if getattr(lock, "write_done_this_turn", False):
            return "Error: write_serial: 本 turn 已有写操作，请先读结果再决策后再写"
    return None


def _mark_write_done(context) -> None:
    service = getattr(context, "exploration_service", None)
    if service is not None:
        service.invalidate("tool_write")
    if getattr(context, "write_serial", False):
        context._write_done_this_turn = True
    lock = _resolve_edit_lock(context)
    if lock is not None and hasattr(lock, "mark_write_done"):
        lock.mark_write_done()


def _near_snippet(text: str, needle: str, *, radius: int = 2) -> str:
    """失败时给出 near= 上下文。"""
    lines = text.splitlines()
    if not lines:
        return "near=(empty file)"
    # 取 needle 首行模糊匹配
    key = (needle or "").splitlines()[0].strip() if needle else ""
    if key:
        for i, ln in enumerate(lines):
            if key[:40] and key[:40] in ln:
                lo = max(0, i - radius)
                hi = min(len(lines), i + radius + 1)
                chunk = "\n".join(f"{j + 1}:{lines[j]}" for j in range(lo, hi))
                return f"near=\n{chunk}"
    # 回退文件头
    head = "\n".join(f"{j + 1}:{lines[j]}" for j in range(min(5, len(lines))))
    return f"near=\n{head}"


def _edit_time_lint_py(path: str, new_text: str) -> str | None:
    """返回错误信息；通过则 None。"""
    if not path.endswith(".py"):
        return None
    try:
        compile(new_text, path, "exec")
    except SyntaxError as e:
        return f"edit_time_lint syntax: {e.msg} (line {e.lineno})"
    return None


def tool_apply_patch(context, args: dict) -> ToolResult:
    """Prepare every mutation through the text core, then commit through the backend."""
    if (routed := _sandbox_workspace_result(context, "apply_patch", args)) is not None:
        return routed
    from agent_runtime.apply_patch_format import parse_apply_patch_text
    from agent_runtime.patch_engine import PatchError, apply_operations
    from agent_runtime.sensitive_paths import is_sensitive_path, sensitive_reject_message
    from agent_runtime.workspace_patches import HostPatchTransaction

    serial_err = _reject_if_write_serial(context)
    if serial_err:
        return ToolResult.error(serial_err, code="tool_execution_failed")
    try:
        ops = parse_apply_patch_text(
            args.get("patch") or args.get("diff") or args.get("input") or ""
        )
        for op in ops:
            target = context.resolve(op.path)
            if is_sensitive_path(target):
                return ToolResult.error(
                    sensitive_reject_message(op.path), code="tool_execution_failed"
                )
            rejected = _reject_if_edit_locked(
                context,
                op.path,
                operation={"add": "create", "delete": "delete"}.get(op.action, "modify"),
            )
            if rejected:
                return ToolResult.error(rejected, code="tool_execution_failed")
        transaction = HostPatchTransaction(context.resolve, [op.path for op in ops])
        transaction.check_hashes(args.get("base_sha256") or args.get("base_hash"))
        changes = apply_operations(transaction.sources(), ops)
        for path, content in changes.items():
            lint_err = _edit_time_lint_py(path, content) if content is not None else None
            if lint_err:
                lock = _resolve_edit_lock(context)
                if lock is not None:
                    lock.edit_lint_reject_count += 1
                return ToolResult.error(f"Error: {lint_err}（未落盘）", code="edit_lint_reject")
        transaction.commit(changes)
    except (ValueError, OSError, UnicodeError) as exc:
        return ToolResult.error(
            f"Error: {getattr(exc, 'code', 'tool_execution_failed')}: {exc}",
            code=exc.code if isinstance(exc, PatchError) else "tool_execution_failed",
        )
    lock = _resolve_edit_lock(context)
    if lock is not None:
        lock.apply_patch_ok_count += len(ops)
    _mark_write_done(context)
    summaries = []
    for path, content in changes.items():
        window = "\n".join(
            f"{i + 1:4d} | {line}" for i, line in enumerate((content or "").splitlines()[:40])
        )
        summaries.append(f"ok apply_patch {path}\n--- 写后窗口 ---\n{window}")
    return ToolResult(content="\n\n".join(summaries))


def tool_quick_test(context, args: dict) -> ToolResult:
    """环内快检：优先跑给定 nodeid / 路径（失败不阻断主环语义，只回灌）。"""
    import subprocess

    from agent_runtime.linux_sandbox.workspace_routing import sandbox_enabled

    if sandbox_enabled(context) and (
        context.sandbox_backend is None
        or getattr(context.sandbox_backend, "workspace_tools_only", False)
    ):
        return sandbox_tool_result_rejection("command sandbox not available")

    nodeid = (args.get("nodeid") or args.get("target") or "").strip()
    path = (args.get("path") or "").strip()
    target = nodeid or path
    if not target:
        return ToolResult.error(
            "Error: quick_test 需要 nodeid 或 path", code="tool_execution_failed"
        )

    if getattr(context, "sandbox_backend", None) is not None:
        from agent_runtime.linux_sandbox.routing import (
            execute_sandbox,
            pytest_target,
            sandbox_tool_result,
        )

        try:
            if getattr(context.sandbox_backend, "guarded_commands", False):
                from agent_runtime.linux_sandbox.command_profile import test_target

                target = test_target(target)
            else:
                target = pytest_target(context.root, target)
            timeout = max(1, min(int(args.get("timeout", 60) or 60), 120))
        except (ValueError, TypeError) as exc:
            return sandbox_tool_result_rejection(str(exc))
        result = execute_sandbox(
            context,
            "pytest",
            (
                "/toolchain/bin/python",
                "-I",
                "-m",
                "pytest",
                target,
                "-q",
                "--tb=line",
                "--maxfail=3",
            ),
            timeout,
        )
        return sandbox_tool_result(result, f"quick_test target={target}")

    root = getattr(context, "root", ".") or "."
    # The target reaches pytest's argv in both tiers, so the host tier applies
    # the same containment and option-injection checks as the sandbox tier --
    # but still accepts a directory, which is a legitimate pytest target here.
    from agent_runtime.linux_sandbox.routing import pytest_target

    try:
        target = pytest_target(root, target, python_file_only=False)
    except (ValueError, TypeError) as exc:
        return ToolResult.error(f"Error: quick_test target 被拒绝: {exc}", code="invalid_args")
    cmd = ["python", "-m", "pytest", target, "-q", "--tb=line", "--maxfail=3"]
    try:
        proc = subprocess.run(
            cmd,
            cwd=str(root),
            capture_output=True,
            text=True,
            timeout=int(args.get("timeout", 60) or 60),
        )
    except subprocess.TimeoutExpired:
        return ToolResult.error(f"Error: quick_test timeout on {target}", code="tool_timeout")
    except OSError as e:
        return ToolResult.error(
            f"Error: quick_test failed to start: {e}", code="tool_execution_failed"
        )

    out = (proc.stdout or "") + ("\n" + proc.stderr if proc.stderr else "")
    out = out.strip()
    if not out:
        out = "(command succeeded with no output)" if proc.returncode == 0 else "(no output)"
    status = "PASS" if proc.returncode == 0 else "FAIL"
    # 截断
    if len(out) > 2500:
        out = out[:2000] + "\n...[truncated]...\n" + out[-400:]
    return ToolResult(content=f"quick_test {status} target={target} exit={proc.returncode}\n{out}")


def tool_grep(context, args: dict):
    if (routed := _sandbox_workspace_result(context, "grep", args)) is not None:
        return routed
    from agent_runtime.code_exploration.io import grep_result

    return grep_result(context, args)


# ============================================================================
# 高风险写工具执行函数
# ============================================================================


def tool_write_file(context, args: dict) -> ToolResult:
    """创建或覆盖文件，自动创建父目录。

    Args 必须包含 'path' 和 'content'，可选 'append'（默认 False）。
    """
    if (routed := _sandbox_workspace_result(context, "write_file", args)) is not None:
        return routed
    raw_path = args.get("path", "")
    if not raw_path:
        return ToolResult.error("Error: 缺少必填参数 path", code="tool_execution_failed")
    content = args.get("content", "")
    append = args.get("append", False)

    from agent_runtime.sensitive_paths import is_sensitive_path, sensitive_reject_message

    if is_sensitive_path(raw_path):
        return ToolResult.error(sensitive_reject_message(raw_path), code="tool_execution_failed")

    serial_err = _reject_if_write_serial(context)
    if serial_err:
        return ToolResult.error(serial_err, code="tool_execution_failed")

    try:
        target = context.resolve(raw_path)
    except ValueError as e:
        return ToolResult.error(f"Error: {e}", code="tool_execution_failed")

    if is_sensitive_path(target):
        return ToolResult.error(sensitive_reject_message(raw_path), code="tool_execution_failed")

    operation = "modify" if target.exists() or target.is_symlink() else "create"
    rejected = _reject_if_edit_locked(context, raw_path, operation=operation)
    if rejected:
        return ToolResult.error(rejected, code="tool_execution_failed")

    from agent_runtime.atomic_io import atomic_write_text

    try:
        if append and target.exists():
            payload = target.read_text(encoding="utf-8") + content
            mode = "已追加到"
        else:
            payload = content
            mode = "已写入"
        atomic_write_text(target, payload, create_only=operation == "create")
    except OSError as e:
        return ToolResult.error(f"Error: 写入文件失败: {e}", code="tool_execution_failed")

    _mark_write_done(context)
    return ToolResult(content=f"{mode} {raw_path}（{len(content)} 字符）")


def tool_patch_file(context, args: dict) -> ToolResult:
    """精确文本替换或 unified diff 多 hunk 修补。

    Args 必须包含 path，以及 diff 或 (old_text + new_text)。
    old_text 必须出现恰好 1 次；diff 支持多个 @@ hunk。
    """
    if (routed := _sandbox_workspace_result(context, "patch_file", args)) is not None:
        return routed
    raw_path = args.get("path", "")
    if not raw_path:
        return ToolResult.error("Error: 缺少必填参数 path", code="tool_execution_failed")

    from agent_runtime.sensitive_paths import is_sensitive_path, sensitive_reject_message

    if is_sensitive_path(raw_path):
        return ToolResult.error(sensitive_reject_message(raw_path), code="tool_execution_failed")

    serial_err = _reject_if_write_serial(context)
    if serial_err:
        return ToolResult.error(serial_err, code="tool_execution_failed")

    rejected = _reject_if_edit_locked(context, raw_path)
    if rejected:
        return ToolResult.error(rejected, code="tool_execution_failed")

    try:
        target = context.resolve(raw_path)
    except ValueError as e:
        return ToolResult.error(f"Error: {e}", code="tool_execution_failed")

    if is_sensitive_path(target):
        return ToolResult.error(sensitive_reject_message(raw_path), code="tool_execution_failed")

    if not target.exists():
        return ToolResult.error(f"Error: 文件不存在: {raw_path}", code="tool_execution_failed")
    if not target.is_file():
        return ToolResult.error(f"Error: 不是文件: {raw_path}", code="tool_execution_failed")

    try:
        text = target.read_bytes().decode("utf-8")
    except UnicodeDecodeError:
        return ToolResult.error(
            f"Error: 无法以 UTF-8 编码读取: {raw_path}", code="tool_execution_failed"
        )

    from agent_runtime.atomic_io import atomic_write_text
    from agent_runtime.patch_engine import build_preview, parse_patch_input

    expected_hash = args.get("base_sha256") or args.get("base_hash")
    if expected_hash:
        import hashlib

        actual_hash = hashlib.sha256(target.read_bytes()).hexdigest()
        if actual_hash != str(expected_hash):
            return ToolResult.error(
                f"Error: stale patch/base hash mismatch: {raw_path}", code="stale_preimage"
            )

    try:
        plan = parse_patch_input(args)
    except ValueError as e:
        return ToolResult.error(f"Error: {e}", code="tool_execution_failed")

    from agent_runtime.patch_engine import PatchError, apply_text

    try:
        new_text = apply_text(text, plan)
    except PatchError as exc:
        return ToolResult.error(f"Error: {exc}", code=exc.code)

    lint_err = _edit_time_lint_py(raw_path, new_text)
    if lint_err:
        lock = _resolve_edit_lock(context)
        if lock is not None:
            lock.edit_lint_reject_count += 1
        return ToolResult.error(f"Error: {lint_err}（未落盘）", code="edit_lint_reject")

    try:
        atomic_write_text(target, new_text)
    except OSError as e:
        return ToolResult.error(f"Error: 写入文件失败: {e}", code="tool_execution_failed")

    _mark_write_done(context)
    preview = build_preview(raw_path, plan)
    delta = preview.lines_added - preview.lines_removed
    lines = new_text.splitlines()
    window = "\n".join(f"{i + 1:4d} | {lines[i]}" for i in range(min(20, len(lines))))
    if preview.hunk_count == 1 and plan.mode == "replacement":
        return ToolResult(
            content=f"已修补 {raw_path}（替换 1 处，{delta:+d} 字符）\n--- 写后窗口 ---\n{window}"
        )
    return ToolResult(
        content=f"已修补 {raw_path}（{preview.hunk_count} 个 hunk，"
        f"-{preview.lines_removed}/+{preview.lines_added} 行）\n"
        f"--- 写后窗口 ---\n{window}"
    )


def tool_run_shell(context, args: dict) -> ToolResult:
    """在 workspace 根目录执行 Shell 命令。

    Args 必须包含 'command'，可选 'timeout'(默认20s)。
    环境变量经过白名单过滤；输出经 redact_text 脱敏。
    """
    from agent_runtime.linux_sandbox.workspace_routing import sandbox_enabled

    if sandbox_enabled(context) and (
        context.sandbox_backend is None
        or getattr(context.sandbox_backend, "workspace_tools_only", False)
    ):
        return sandbox_tool_result_rejection("command sandbox not available")
    from agent_runtime.security import check_shell_command, parse_shell_argv, redact_text
    from agent_runtime.security import shell_env as _shell_env

    command = args.get("command", "")
    if not command:
        return ToolResult.error("Error: 缺少必填参数 command", code="tool_execution_failed")

    allowed, reason = check_shell_command(command)
    if not allowed:
        return ToolResult.error(
            f"Error: Shell 命令被安全策略拒绝 ({reason}): {command[:100]}",
            code="tool_execution_failed",
        )
    try:
        timeout = int(args.get("timeout", 20))
    except (ValueError, TypeError):
        timeout = 20
    timeout = max(1, min(timeout, 120))  # 限制 1-120 秒

    root = context.root
    try:
        argv = parse_shell_argv(command)
    except ValueError as exc:
        return ToolResult.error(
            f"Error: Shell 命令被安全策略拒绝 ({exc})", code="tool_execution_failed"
        )
    if getattr(context, "sandbox_backend", None) is not None:
        from agent_runtime.linux_sandbox.routing import execute_sandbox, sandbox_tool_result

        if argv[0] not in {"python", "python3", "py"}:
            return sandbox_tool_result_rejection("executable not in Python sandbox profile")
        result = execute_sandbox(
            context,
            "command",
            ("/toolchain/bin/python", *argv[1:]),
            timeout,
        )
        return sandbox_tool_result(result, "run_shell")
    if os.name == "nt":
        # Windows built-ins (echo/set) and Python shims are resolved by
        # cmd.exe.  The command line is generated from parsed argv, never
        # concatenated from raw user text, so shell operators cannot escape.
        comspec = os.environ.get("COMSPEC", "cmd.exe")
        if argv[0].lower() == "set":
            argv = [comspec, "/d", "/s", "/c", "set"]
        elif argv[0].lower().rsplit("\\", 1)[-1].removesuffix(".exe") in {
            "python",
            "python3",
            "py",
        }:
            # Avoid a cmd.exe intermediary for Python probes so cancellation
            # can terminate the process directly and deterministically.
            argv = [sys.executable, *argv[1:]]
        else:
            argv = [comspec, "/d", "/s", "/c", subprocess.list2cmdline(argv)]
    provider = getattr(context, "shell_env_provider", None)
    if callable(provider):
        env = provider()
    else:
        env = _shell_env(root=root)

    cancel_token = getattr(context, "cancel_token", None)
    result = _run_shell(argv, command, root, env, timeout, cancel_token)
    result.content = redact_text(result.content)
    return result


def sandbox_tool_result_rejection(reason: str):
    from agent_runtime.tool_result import ToolResult, ToolStatus

    return ToolResult(
        content=f"Error: sandbox policy denied: {reason}",
        status=ToolStatus.REJECTED.value,
        error_code="policy_denied",
        retryable=False,
        metadata={"execution_tier": "none", "requested_backend": "wsl_bwrap"},
    )


def _kill_process_tree(proc: subprocess.Popen) -> bool:
    """Stop the owned process group and report whether termination was confirmed."""
    import signal

    if os.name == "nt":
        killed = (
            subprocess.run(
                ["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                capture_output=True,
                check=False,
            ).returncode
            == 0
        )
    else:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
            killed = True
        except ProcessLookupError:
            killed = proc.poll() is not None
    if proc.poll() is None:
        proc.kill()
    try:
        proc.wait(timeout=1)
    except subprocess.TimeoutExpired:
        return False
    return killed


def _run_shell(
    argv: list[str], display_command: str, root, env, timeout: int, cancel_token=None
) -> ToolResult:
    """Drain both pipes continuously; retained output is bounded independently of runtime."""
    import threading
    import time

    from agent_runtime.io_limits import shell_max_bytes

    if cancel_token is not None and cancel_token.is_cancelled:
        return ToolResult(
            content=f"Error: 命令已取消: {display_command[:100]}",
            status=ToolStatus.CANCELLED,
            error_code="tool_cancelled",
            retryable=False,
            metadata={"termination_guaranteed": True},
        )
    options = (
        {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP}
        if os.name == "nt"
        else {"start_new_session": True}
    )
    proc = subprocess.Popen(
        argv,
        cwd=root,
        shell=False,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=env,
        **options,
    )
    limit = max(1, shell_max_bytes())
    buffers = [bytearray(), bytearray()]
    truncated = [False, False]
    reader_errors = []

    def drain(pipe, index):
        try:
            with pipe:
                while chunk := pipe.read1(65536):
                    available = max(0, limit - len(buffers[index]))
                    buffers[index].extend(chunk[:available])
                    truncated[index] |= len(chunk) > available
        except OSError as exc:
            reader_errors.append(str(exc))

    readers = [
        threading.Thread(target=drain, args=(pipe, i), daemon=True)
        for i, pipe in enumerate((proc.stdout, proc.stderr))
    ]
    for reader in readers:
        reader.start()
    deadline = time.monotonic() + timeout if timeout > 0 else None
    failure = ""
    termination_confirmed = True
    try:
        while proc.poll() is None or any(reader.is_alive() for reader in readers):
            if cancel_token is not None and cancel_token.is_cancelled:
                failure = "tool_cancelled"
            elif deadline is not None and time.monotonic() >= deadline:
                failure = "tool_timeout"
            if failure:
                termination_confirmed = _kill_process_tree(proc)
                break
            time.sleep(0.02)
    finally:
        if proc.poll() is None:
            termination_confirmed = _kill_process_tree(proc)
        for reader in readers:
            reader.join(timeout=1)
    drained = not any(reader.is_alive() for reader in readers)
    output = [bytes(buf).decode("utf-8", errors="replace") for buf in buffers]
    for i, label in enumerate(("stdout", "stderr")):
        if truncated[i]:
            output[i] += f"\n... [{label} truncated at {limit} bytes]"
    result = _format_shell_result(proc.returncode, *output)
    result.output_truncated |= any(truncated)
    result.metadata["termination_guaranteed"] = termination_confirmed and drained
    if failure:
        result.status = ToolStatus.CANCELLED if failure == "tool_cancelled" else ToolStatus.ERROR
        result.error_code = failure
        result.retryable = False
        reason = "取消" if failure == "tool_cancelled" else f"超时（{timeout} 秒）"
        result.content = f"Error: 命令已{reason}: {display_command[:100]}\n{result.content}"
    if not drained or reader_errors:
        result.status = ToolStatus.UNCERTAIN
        result.error_code = "cleanup_unverified" if not drained else "output_read_failed"
        result.retryable = False
    return result


def _format_shell_result(returncode: int, stdout: str, stderr: str) -> ToolResult:
    from agent_runtime.io_limits import shell_max_bytes, truncate_text

    stdout, stdout_cut = truncate_text(stdout, shell_max_bytes(), label="stdout")
    stderr, stderr_cut = truncate_text(stderr, shell_max_bytes(), label="stderr")
    out = []
    out.append(f"exit_code: {returncode}")
    if stdout.strip():
        out.append(f"stdout:\n{stdout.rstrip()}")
    if stderr.strip():
        out.append(f"stderr:\n{stderr.rstrip()}")
    return ToolResult(
        content="\n".join(out),
        data={"exit_code": returncode},
        output_truncated=stdout_cut or stderr_cut,
    )


# ============================================================================
# 工具注册表
# ============================================================================


def tool_expand_observation(context: ToolContext, args: dict) -> ToolResult:
    """Return a bounded, checksum-validated Observation payload."""
    from agent_runtime.context_runtime import ObservationStore

    state = context.observation_state
    if not isinstance(state, dict):
        return ToolResult(
            content="Observation expansion unavailable: session state is not attached."
        )
    observation_id = str(args.get("observation_id", "") or "")
    if not observation_id.startswith("OBS-"):
        return ToolResult(content="Observation expansion denied: invalid observation id.")
    store = ObservationStore(
        state,
        root=context.root,
        state_root=str(getattr(context, "state_root", "") or ""),
    )
    try:
        result = store.expand_for_context(
            observation_id,
            max_tokens=max(1, min(int(args.get("max_tokens", 2000) or 2000), 8000)),
            actor="tool:expand_observation",
            context=context,
        )
    finally:
        store.close()
    from agent_runtime.code_exploration.consumption import retrieval_header

    contract = result.get("retrieval_result")
    header = retrieval_header(contract, result.get("freshness", "unknown")) if contract else ""
    if not result.get("ok"):
        reason = result.get("reason", "unknown")
        return ToolResult(content=f"{header}Observation unavailable: {reason}; reread required.")
    return ToolResult(
        content=f"[{result['observation_id']}] tool={result.get('tool', '')} "
        f"source_version={result.get('source_version', '')} "
        f"output_truncated={str(result['output_truncated']).lower()}\n"
        f"{header}{result.get('content', '')}"
    )


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
        "budget_group": "read",
        "schema": auto_schema(ListFilesArgs),
        "risky": False,
        "execution_tier": TIER_HOST,
        "description": "列出目录内容。参数: path（默认 '.'）",
        "run": lambda args: tool_list_files(context, args),
    }

    # ---- read_file ----
    registry["read_file"] = {
        "budget_group": "read",
        "schema": auto_schema(ReadFileArgs),
        "risky": False,
        "execution_tier": TIER_HOST,
        "description": "按行号范围读取 UTF-8 文件。参数: path, start(默认1), end(默认200)",
        "run": lambda args: tool_read_file(context, args),
    }

    # ---- grep ----
    registry["grep"] = {
        "budget_group": "read",
        "schema": auto_schema(GrepArgs),
        "risky": False,
        "execution_tier": TIER_HOST,
        "description": (
            "内容搜索（rg 优先，Python fallback）。"
            "参数: pattern, path, glob, ignore_case, context_lines, max_results"
        ),
        "run": lambda args: tool_grep(context, args),
    }

    registry["code_lookup"] = {
        "budget_group": "read",
        "schema": auto_schema(CodeLookupArgs),
        "risky": False,
        "execution_tier": TIER_HOST,
        "description": (
            "查询 Python 符号定义或引用。path、line、column 为已落盘文件的 1 起始精确位置。"
        ),
        "run": lambda args: _code_lookup_structured(context, args),
    }

    registry["code_relations"] = {
        "budget_group": "read",
        "schema": auto_schema(CodeRelationsArgs),
        "risky": False,
        "execution_tier": TIER_HOST,
        "description": "整理本次任务已观察的 Python 文件、符号与导入/引用关系。",
        "run": lambda args: _code_relations_structured(context, args),
    }

    # ---- write_file ----
    registry["write_file"] = {
        "budget_group": "write",
        "schema": auto_schema(WriteFileArgs),
        "risky": True,
        "execution_tier": TIER_HOST,
        "description": ("【最后手段】整文件覆盖创建；修复优先用 apply_patch。参数: path, content"),
        "run": lambda args: tool_write_file(context, args),
    }

    # ---- patch_file ----
    registry["patch_file"] = {
        "budget_group": "write",
        "schema": auto_schema(PatchFileArgs),
        "risky": True,
        "execution_tier": TIER_HOST,
        "description": (
            "单点精确替换（old_text 恰好 1 次）；多行/多处改动优先 apply_patch。"
            "参数: path, old_text, new_text"
        ),
        "run": lambda args: tool_patch_file(context, args),
    }

    # ---- apply_patch ----
    registry["apply_patch"] = {
        "budget_group": "write",
        "schema": auto_schema(ApplyPatchArgs),
        "risky": True,
        "execution_tier": TIER_HOST,
        "description": (
            "【首选写入】Codex 风格：*** Begin Patch / *** Update File: path / "
            "@@ hunk（须含 - 或上下文行）/ *** End Patch。先 read_file。参数: patch"
        ),
        "run": lambda args: tool_apply_patch(context, args),
    }

    # ---- quick_test ----
    registry["quick_test"] = {
        "budget_group": "verify",
        "schema": auto_schema(QuickTestArgs),
        "risky": context.sandbox_backend is not None,
        "execution_tier": "linux_sandbox" if context.sandbox_backend is not None else TIER_HOST,
        "description": "环内快检 pytest nodeid/path。参数: nodeid 或 path, timeout",
        # nodeid 优先于 path，因此它必须和 path 一样受参数治理约束。
        # A list, not a frozenset: the execution registry is serialised into eval
        # manifests, and `bind_execution_tools` normalises ToolSpec declarations
        # to sorted lists for the same reason.
        "argv_args": ["nodeid"],
        "run": lambda args: tool_quick_test(context, args),
    }

    # ---- run_shell ----
    registry["run_shell"] = {
        "budget_group": "verify",
        "schema": auto_schema(RunShellArgs),
        "risky": True,
        "execution_tier": "linux_sandbox" if context.sandbox_backend is not None else TIER_HOST,
        "description": "执行 Shell 命令。参数: command, timeout(默认20s，最大120s)",
        "run": lambda args: tool_run_shell(context, args),
    }

    registry["expand_observation"] = {
        "budget_group": "read",
        "schema": auto_schema(ExpandObservationArgs),
        "risky": False,
        "execution_tier": TIER_HOST,
        "description": "按 OBS-* 引用展开受治理 Tool 证据。参数: observation_id, max_tokens",
        "run": lambda args: tool_expand_observation(context, args),
    }

    # Audited effects belong to the trusted registry, never to model output.
    readonly = {
        "list_files",
        "read_file",
        "grep",
        "code_lookup",
        "code_relations",
        "expand_observation",
    }
    for name, spec in registry.items():
        spec["side_effect"] = (
            "read"
            if name in readonly
            else "verify"
            if name in {"quick_test", "run_shell"}
            else "write"
        )
    # Audited pure Python readers: no subprocess, bounded IO/output and
    # cooperative cancellation. Edit-lock updates happen on the loop owner.
    from agent_runtime.batch_reads import audited_read_file
    from agent_runtime.code_exploration.io import list_files_result

    for name, handler in (("read_file", audited_read_file), ("list_files", list_files_result)):
        registry[name]["run_with_context"] = handler
        registry[name]["parallel_run"] = registry[name]["run"]
    from agent_runtime.linux_sandbox.workspace_routing import bind_workspace_tools

    return bind_workspace_tools(context, registry)


def legal_tool_names(registry: dict) -> set[str]:
    """返回注册表中所有可调用工具名的集合。"""
    return set(registry.keys())
