"""工具定义：参数 dataclass + 执行函数 + 工具注册。

每个工具由两部分组成：
1. 参数 dataclass — 定义工具接受的参数及其类型和默认值
2. 执行函数 — 接受 ToolContext + dict，执行工具逻辑，返回结果字符串

auto_schema() 从 dataclass 自动推导参数字典，新增工具无需手写 schema。
"""

import json
import os
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

from agent_runtime.schema_utils import auto_schema
from agent_runtime.tool_context import ToolContext

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
class ExpandLockArgs:
    """显式扩锁。"""

    path: str = ""


@dataclass
class FinishRepairArgs:
    """End a repair attempt without claiming that a patch was produced."""

    status: str = ""
    reason: str = ""


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
    """Legacy string surface for bounded file enumeration."""
    from agent_runtime.code_exploration.io import list_files_result

    return list_files_result(context, args).content


def _list_files_structured(context, args: dict):
    from agent_runtime.code_exploration.io import list_files_result

    return list_files_result(context, args)


def _code_lookup_structured(context, args: dict):
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


def tool_read_file(context, args: dict) -> str:
    """Legacy string surface for bounded range reads."""
    from agent_runtime.code_exploration.io import read_file_result

    result = read_file_result(context, args)
    if result.ok:
        _mark_edit_lock_read(context, str(args.get("path", "")))
    return result.content


def _read_file_structured(context, args: dict):
    from agent_runtime.code_exploration.io import read_file_result

    result = read_file_result(context, args)
    if result.ok:
        _mark_edit_lock_read(context, str(args.get("path", "")))
    return result


def _resolve_edit_lock(context):
    lock = getattr(context, "edit_lock", None)
    if lock is not None:
        return lock
    try:
        from src.repair.execution.edit_lock import get_active_edit_lock

        return get_active_edit_lock(getattr(context, "root", None))
    except Exception:
        return None


def _mark_edit_lock_read(context, raw_path: str) -> None:
    lock = _resolve_edit_lock(context)
    if lock is None:
        return
    try:
        lock.mark_read(raw_path, auto_allow_impl=True)
    except TypeError:
        try:
            lock.mark_read(raw_path)
        except Exception:
            pass
    except Exception:
        pass


def _reject_if_edit_locked(context, raw_path: str) -> str | None:
    lock = _resolve_edit_lock(context)
    if lock is None:
        return None
    try:
        ok, reason = lock.check_write(raw_path)
    except Exception:
        return None
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


def _check_diff_preimage(file_text: str, plan) -> str | None:
    """校验 unified hunk 删除/上下文行是否匹配当前文件。"""
    import re

    if getattr(plan, "mode", None) != "diff":
        return None
    lines = file_text.splitlines()
    for hunk in plan.hunks or []:
        m = re.match(r"@@ -(\d+)(?:,\d+)? \+(\d+)(?:,\d+)? @@", hunk.header)
        if not m:
            return "bad_hunk_header"
        idx = int(m.group(1)) - 1
        for kind, text in hunk.lines:
            if kind in (" ", "-"):
                if idx >= len(lines):
                    return "past_eof"
                if lines[idx] != text:
                    return f"line_{idx + 1}_mismatch"
                idx += 1
            # '+' 不前进 old idx
    return None


def _edit_time_lint_py(path: str, new_text: str) -> str | None:
    """返回错误信息；通过则 None。"""
    if not path.endswith(".py"):
        return None
    try:
        compile(new_text, path, "exec")
    except SyntaxError as e:
        return f"edit_time_lint syntax: {e.msg} (line {e.lineno})"
    return None


def _normalize_hunk_headers(diff: str, file_text: str) -> str:
    """Lenient：补全裸 ``@@`` / 无 @@ 的 +/- 块为可应用 unified hunk。"""
    import re

    d = (diff or "").strip()
    if not d:
        return d

    def _locate(body: str, *, minimum_line: int = 1) -> tuple[int, int, int]:
        lines = body.splitlines()
        first_preimage = next(
            (
                index
                for index, line in enumerate(lines)
                if line.startswith(" ") or (line.startswith("-") and not line.startswith("---"))
            ),
            0,
        )
        first_removed = next(
            (
                index
                for index, line in enumerate(lines)
                if line.startswith("-") and not line.startswith("---")
            ),
            None,
        )
        anchor_index = first_removed if first_removed is not None else first_preimage
        needle = next(
            (x[1:] for x in lines if x.startswith("-") and not x.startswith("---")),
            lines[first_preimage][1:] if lines and lines[first_preimage].startswith(" ") else "",
        )
        start = max(1, minimum_line)
        if needle:
            for i, fl in enumerate(file_text.splitlines(), 1):
                if i < start:
                    continue
                if fl == needle or needle in fl:
                    prefix = sum(
                        1
                        for line in lines[:anchor_index]
                        if line.startswith(" ")
                        or (line.startswith("-") and not line.startswith("---"))
                    )
                    start = max(1, i - prefix)
                    break
        old_n = (
            sum(
                1
                for x in lines
                if x.startswith(" ") or (x.startswith("-") and not x.startswith("---"))
            )
            or 1
        )
        new_n = (
            sum(
                1
                for x in lines
                if x.startswith(" ") or (x.startswith("+") and not x.startswith("+++"))
            )
            or 1
        )
        return start, old_n, new_n

    if "@@" not in d:
        start, old_n, new_n = _locate(d)
        return f"@@ -{start},{old_n} +{start},{new_n} @@\n{d}"

    hunk_re = re.compile(r"^@@ -\d+(?:,\d+)? \+\d+(?:,\d+)? @@")
    out_lines: list[str] = []
    current_header = ""
    current_body: list[str] = []
    search_from = 1

    def _flush_hunk() -> None:
        nonlocal search_from
        if not current_header:
            return
        header = current_header.strip()
        if not hunk_re.match(header):
            start, old_n, new_n = _locate("\n".join(current_body), minimum_line=search_from)
            header = f"@@ -{start},{old_n} +{start},{new_n} @@"
            consumed = sum(
                1
                for line in current_body
                if line.startswith((" ", "-")) and not line.startswith("---")
            )
            search_from = start + max(1, consumed)
        else:
            match = re.match(r"^@@ -(\d+)(?:,(\d+))? \+", header)
            if match:
                search_from = int(match.group(1)) + int(match.group(2) or 1)
        out_lines.append(header)
        out_lines.extend(current_body)

    for ln in d.splitlines():
        if ln.strip().startswith("@@"):
            _flush_hunk()
            current_header = ln
            current_body = []
        elif current_header:
            current_body.append(ln)
        else:
            out_lines.append(ln)
    _flush_hunk()
    return "\n".join(out_lines)


def tool_expand_lock(context, args: dict) -> str:
    """显式扩锁：将路径加入 allowed_edit（最多 2 次）；扩后须 read 再写。"""
    raw_path = args.get("path", "")
    if not raw_path:
        return "Error: 缺少必填参数 path"
    lock = _resolve_edit_lock(context)
    if lock is None:
        return "Error: expand_lock 需要 active edit_lock（patcher_primary）"
    ok, reason = lock.expand_lock(raw_path)
    if not ok:
        return f"Error: expand_lock failed ({reason})"
    return (
        f"expand_lock ok: {reason}. "
        f"allowed_edit={sorted(lock.allowed_edit)[:12]}. "
        "下一步: read_file 该路径后再 apply_patch/patch_file。"
    )


def _patch_transaction_paths(context, patch_text: str) -> dict:
    """Capture only paths named by an apply_patch envelope."""
    import re

    snapshot: dict = {}
    for raw in re.findall(r"\*\*\*\s+(?:Update|Add|Delete) File:\s*(.+)", patch_text or ""):
        try:
            target = context.resolve(raw.strip())
        except ValueError:
            continue
        try:
            stat = target.lstat() if target.exists() else None
            snapshot[str(target)] = {
                "exists": bool(stat),
                "bytes": target.read_bytes() if stat and target.is_file() else None,
                "mode": stat.st_mode if stat else None,
            }
        except OSError:
            snapshot[str(target)] = {"exists": False, "bytes": None, "mode": None}
    return snapshot


def _restore_patch_transaction(snapshot: dict) -> None:
    for raw_path, before in snapshot.items():
        target = Path(raw_path)
        try:
            if before["exists"]:
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(before["bytes"] or b"")
                if before.get("mode") is not None:
                    target.chmod(before["mode"])
            elif target.exists() or target.is_symlink():
                target.unlink()
        except OSError:
            # Preserve the original patch error; the caller records rollback
            # failure through the returned metadata/trace path.
            pass


def tool_apply_patch(context, args: dict) -> str:
    """Transactional wrapper around the canonical apply_patch implementation."""
    patch_text = args.get("patch") or args.get("diff") or args.get("input") or ""
    snapshot = _patch_transaction_paths(context, str(patch_text))
    expected = args.get("base_sha256") or args.get("base_hash")
    if expected:
        expected_by_path = expected if isinstance(expected, dict) else {}
        for raw_path in expected_by_path or {"": expected}:
            if isinstance(expected, dict):
                value = expected_by_path.get(raw_path)
                try:
                    target = context.resolve(raw_path)
                except ValueError:
                    return f"Error: base hash path invalid: {raw_path}"
            else:
                value = expected
                target = next((Path(path) for path in snapshot), None)
            if target is None or not target.is_file():
                return "Error: base hash target missing"
            import hashlib

            actual = hashlib.sha256(target.read_bytes()).hexdigest()
            if actual != str(value):
                return f"Error: stale patch/base hash mismatch: {target}"
    result = _tool_apply_patch_unchecked(context, args)
    if result.startswith("Error:"):
        _restore_patch_transaction(snapshot)
        try:
            from agent_runtime.metrics import get_registry

            reason = "stale_patch" if "stale" in result.lower() else "apply_error"
            metric = (
                "fixloop_stale_patch_rejections_total"
                if reason == "stale_patch"
                else "fixloop_patch_rollbacks_total"
            )
            get_registry().counter_inc(metric, labels={"reason": reason})
        except Exception:
            pass
    return result


def _tool_apply_patch_unchecked(context, args: dict) -> str:
    """Codex 风格 apply_patch：*** Begin/End Patch；ACI 写后回显 + edit-time lint。"""
    from agent_runtime.apply_patch_format import parse_apply_patch_text
    from agent_runtime.atomic_io import atomic_write_text
    from agent_runtime.patch_engine import apply_plan, parse_patch_input
    from agent_runtime.sensitive_paths import is_sensitive_path, sensitive_reject_message

    patch_text = args.get("patch") or args.get("diff") or args.get("input") or ""
    if not str(patch_text).strip():
        return "Error: apply_patch 缺少 patch 文本（*** Begin Patch ... *** End Patch）"

    serial_err = _reject_if_write_serial(context)
    if serial_err:
        return serial_err

    try:
        ops = parse_apply_patch_text(str(patch_text))
    except ValueError as e:
        return f"Error: {e}"

    summaries: list[str] = []
    for op in ops:
        raw_path = op.path
        if is_sensitive_path(raw_path):
            return sensitive_reject_message(raw_path)
        rejected = _reject_if_edit_locked(context, raw_path)
        if rejected:
            return rejected
        try:
            target = context.resolve(raw_path)
        except ValueError as e:
            return f"Error: {e}"

        if op.action == "delete":
            if not target.exists():
                return f"Error: delete 目标不存在: {raw_path}"
            try:
                target.unlink()
            except OSError as e:
                return f"Error: 删除失败: {e}"
            summaries.append(f"deleted {raw_path}")
            continue

        if op.action == "add":
            content = op.diff if op.diff.endswith("\n") else op.diff + "\n"
            lint_err = _edit_time_lint_py(raw_path, content)
            if lint_err:
                lock = _resolve_edit_lock(context)
                if lock is not None:
                    lock.edit_lint_reject_count += 1
                return f"Error: {lint_err}"
            try:
                target.parent.mkdir(parents=True, exist_ok=True)
                atomic_write_text(target, content)
            except OSError as e:
                return f"Error: 写入失败: {e}"
            summaries.append(f"added {raw_path} ({len(content)} chars)")
            continue

        # update
        if not target.is_file():
            return f"Error: 文件不存在: {raw_path}"
        try:
            text = target.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            return f"Error: 无法以 UTF-8 读取: {raw_path}"

        from agent_runtime.apply_patch_format import update_diff_has_preimage

        if not update_diff_has_preimage(op.diff or ""):
            near = _near_snippet(text, "")
            return (
                f"Error: apply_patch empty_original（Update 缺少 - / 上下文行）。"
                f"先 read_file {raw_path} 再带 preimage 重试。{near}"
            )

        diff = _normalize_hunk_headers(op.diff, text)
        try:
            plan = parse_patch_input({"diff": diff})
        except ValueError as e:
            return f"Error: {e}"

        # Preimage 校验：删除行须与文件当前内容一致（防静默错位改写）
        pre_err = _check_diff_preimage(text, plan)
        if pre_err:
            removed = "\n".join(
                ln[1:]
                for ln in diff.splitlines()
                if ln.startswith("-") and not ln.startswith("---")
            )
            near = _near_snippet(text, removed)
            return (
                f"Error: apply_patch stale/未匹配（{pre_err}）。先 read_file 再 apply_patch。{near}"
            )

        new_text = apply_plan(text, plan)
        if new_text is None:
            removed = "\n".join(
                ln[1:]
                for ln in diff.splitlines()
                if ln.startswith("-") and not ln.startswith("---")
            )
            near = _near_snippet(text, removed)
            return f"Error: apply_patch stale/未匹配（hunk 与文件不一致）。{near}"

        lint_err = _edit_time_lint_py(raw_path, new_text)
        if lint_err:
            lock = _resolve_edit_lock(context)
            if lock is not None:
                lock.edit_lint_reject_count += 1
            return f"Error: {lint_err}（未落盘）"

        try:
            atomic_write_text(target, new_text)
        except OSError as e:
            return f"Error: 写入失败: {e}"

        lock = _resolve_edit_lock(context)
        if lock is not None:
            lock.apply_patch_ok_count += 1

        # 写后窗口回显（ACI）
        lines = new_text.splitlines()
        window = "\n".join(f"{i + 1:4d} | {lines[i]}" for i in range(min(40, len(lines))))
        summaries.append(
            f"ok apply_patch {raw_path} ({len(text.splitlines())}→{len(lines)} lines)\n"
            f"--- 写后窗口 ---\n{window}"
        )

    _mark_write_done(context)
    return "\n\n".join(summaries) if summaries else "Error: apply_patch 无有效操作"


def tool_quick_test(context, args: dict) -> str:
    """环内快检：优先跑给定 nodeid / 路径（失败不阻断主环语义，只回灌）。"""
    import subprocess

    nodeid = (args.get("nodeid") or args.get("target") or "").strip()
    path = (args.get("path") or "").strip()
    target = nodeid or path
    if not target:
        return "Error: quick_test 需要 nodeid 或 path"

    if getattr(context, "sandbox_backend", None) is not None:
        from agent_runtime.linux_sandbox.routing import (
            execute_sandbox,
            pytest_target,
            sandbox_tool_result,
        )

        try:
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
        return f"Error: quick_test timeout on {target}"
    except OSError as e:
        return f"Error: quick_test failed to start: {e}"

    out = (proc.stdout or "") + ("\n" + proc.stderr if proc.stderr else "")
    out = out.strip()
    if not out:
        out = "(command succeeded with no output)" if proc.returncode == 0 else "(no output)"
    status = "PASS" if proc.returncode == 0 else "FAIL"
    # 截断
    if len(out) > 2500:
        out = out[:2000] + "\n...[truncated]...\n" + out[-400:]
    return f"quick_test {status} target={target} exit={proc.returncode}\n{out}"


def tool_search(context, args: dict) -> str:
    """代码搜索（已委托 grep，保留兼容名。新调用请直接用 grep）。"""
    return tool_grep(context, args)


def tool_grep(context, args: dict) -> str:
    """Legacy string surface for budgeted text search."""
    from agent_runtime.code_exploration.io import grep_result

    return grep_result(context, args).content


def _grep_structured(context, args: dict):
    from agent_runtime.code_exploration.io import grep_result

    return grep_result(context, args)


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

    serial_err = _reject_if_write_serial(context)
    if serial_err:
        return serial_err

    rejected = _reject_if_edit_locked(context, raw_path)
    if rejected:
        return rejected

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

    _mark_write_done(context)
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

    serial_err = _reject_if_write_serial(context)
    if serial_err:
        return serial_err

    rejected = _reject_if_edit_locked(context, raw_path)
    if rejected:
        return rejected

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

    expected_hash = args.get("base_sha256") or args.get("base_hash")
    if expected_hash:
        import hashlib

        actual_hash = hashlib.sha256(target.read_bytes()).hexdigest()
        if actual_hash != str(expected_hash):
            return f"Error: stale patch/base hash mismatch: {raw_path}"

    try:
        plan = parse_patch_input(args)
    except ValueError as e:
        return f"Error: {e}"

    if plan.mode == "legacy":
        count = text.count(plan.old_text)
        if count == 0:
            near = _near_snippet(text, plan.old_text)
            return (
                f"Error: old_text 在文件中未找到（出现 0 次）。old_text 必须恰好出现 1 次。{near}"
            )
        if count > 1:
            return f"Error: old_text 出现 {count} 次，必须恰好出现 1 次。请提供更多上下文使其唯一。"

    new_text = apply_plan(text, plan)
    if new_text is None:
        near = _near_snippet(text, getattr(plan, "old_text", "") or "")
        return f"Error: 补丁无法应用到文件（hunk 与文件内容不匹配 / stale）。{near}"

    lint_err = _edit_time_lint_py(raw_path, new_text)
    if lint_err:
        lock = _resolve_edit_lock(context)
        if lock is not None:
            lock.edit_lint_reject_count += 1
        return f"Error: {lint_err}（未落盘）"

    try:
        atomic_write_text(target, new_text)
    except OSError as e:
        return f"Error: 写入文件失败: {e}"

    _mark_write_done(context)
    preview = build_preview(raw_path, plan)
    delta = preview.lines_added - preview.lines_removed
    lines = new_text.splitlines()
    window = "\n".join(f"{i + 1:4d} | {lines[i]}" for i in range(min(20, len(lines))))
    if preview.hunk_count == 1 and plan.mode == "legacy":
        return f"已修补 {raw_path}（替换 1 处，{delta:+d} 字符）\n--- 写后窗口 ---\n{window}"
    return (
        f"已修补 {raw_path}（{preview.hunk_count} 个 hunk，"
        f"-{preview.lines_removed}/+{preview.lines_added} 行）\n"
        f"--- 写后窗口 ---\n{window}"
    )


def tool_run_shell(context, args: dict) -> str:
    """在 workspace 根目录执行 Shell 命令。

    Args 必须包含 'command'，可选 'timeout'(默认20s)。
    环境变量经过白名单过滤；输出经 redact_text 脱敏。
    """
    from agent_runtime.security import check_shell_command, parse_shell_argv, redact_text
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
    try:
        argv = parse_shell_argv(command)
    except ValueError as exc:
        return f"Error: Shell 命令被安全策略拒绝 ({exc})"
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
    if cancel_token is not None:
        return redact_text(_run_shell_cancellable(argv, command, root, env, timeout, cancel_token))
    return redact_text(_run_shell_blocking(argv, command, root, env, timeout))


def sandbox_tool_result_rejection(reason: str):
    from agent_runtime.tool_result import ToolResult, ToolStatus

    return ToolResult(
        content=f"Error: sandbox policy denied: {reason}",
        status=ToolStatus.REJECTED.value,
        error_code="policy_denied",
        retryable=False,
        metadata={"execution_tier": "none", "requested_backend": "wsl_bwrap"},
    )


def _run_shell_blocking(argv: list[str], display_command: str, root, env, timeout: int) -> str:
    try:
        result = subprocess.run(
            argv,
            cwd=root,
            shell=False,
            capture_output=True,
            text=True,
            timeout=timeout,
            env=env,
        )
    except subprocess.TimeoutExpired:
        return f"Error: 命令超时（{timeout} 秒）: {display_command[:100]}"

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


def _run_shell_cancellable(
    argv: list[str], display_command: str, root, env, timeout: int, cancel_token
) -> str:
    import time

    popen_kwargs = {}
    popen_command = argv
    use_shell = False
    if os.name == "nt":
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
            return f"Error: 命令已取消: {display_command[:100]}"
        if time.time() >= deadline:
            _kill_process_tree(proc)
            return f"Error: 命令超时（{timeout} 秒）: {display_command[:100]}"
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


def tool_finish_repair(args: dict) -> str:
    """Return an explicit, machine-readable no-patch terminal outcome."""
    status = str(args.get("status") or "").strip().lower()
    reason = str(args.get("reason") or "").strip()
    if status not in {"cannot_patch", "needs_more_context"}:
        return "Error: finish_repair status 必须是 cannot_patch 或 needs_more_context"
    if not reason:
        return "Error: finish_repair reason 不能为空，必须说明当前证据或缺失上下文"
    return json.dumps({"status": status, "reason": reason}, ensure_ascii=False)


def tool_expand_observation(context: ToolContext, args: dict) -> str:
    """Return a bounded, checksum-validated Observation payload."""
    from agent_runtime.context_runtime import ObservationStore

    state = context.observation_state
    if not isinstance(state, dict):
        return "Observation expansion unavailable: session state is not attached."
    observation_id = str(args.get("observation_id", "") or "")
    if not observation_id.startswith("OBS-"):
        return "Observation expansion denied: invalid observation id."
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
        )
    finally:
        store.close()
    if not result.get("ok"):
        return f"Observation unavailable: {result.get('reason', 'unknown')}"
    return (
        f"[{result['observation_id']}] tool={result.get('tool', '')} "
        f"source_version={result.get('source_version', '')}\n{result.get('content', '')}"
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
        "run": lambda args: _list_files_structured(context, args),
    }

    # ---- read_file ----
    registry["read_file"] = {
        "budget_group": "read",
        "schema": auto_schema(ReadFileArgs),
        "risky": False,
        "execution_tier": TIER_HOST,
        "description": "按行号范围读取 UTF-8 文件。参数: path, start(默认1), end(默认200)",
        "run": lambda args: _read_file_structured(context, args),
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
        "run": lambda args: _grep_structured(context, args),
    }

    # ---- search ----
    registry["search"] = {
        "budget_group": "read",
        "schema": auto_schema(SearchArgs),
        "risky": False,
        "execution_tier": TIER_HOST,
        "description": "代码搜索（rg 优先，Python fallback）。参数: pattern, path（默认 '.'）",
        "run": lambda args: _grep_structured(context, args),
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

    # ---- finish_repair ----
    registry["finish_repair"] = {
        "budget_group": "recovery",
        "schema": auto_schema(FinishRepairArgs),
        "risky": False,
        "terminal": True,
        "execution_tier": TIER_HOST,
        "description": (
            "结构化结束本次修复且不声称已生成补丁。"
            "status 只能是 cannot_patch 或 needs_more_context；reason 必须说明证据。"
        ),
        "run": tool_finish_repair,
    }

    # ---- expand_lock ----
    registry["expand_lock"] = {
        "budget_group": "recovery",
        "schema": auto_schema(ExpandLockArgs),
        "risky": False,
        "execution_tier": TIER_HOST,
        "description": "扩锁：将路径加入 allowed_edit（最多2次），随后须 read 再写。参数: path",
        "run": lambda args: tool_expand_lock(context, args),
    }

    # ---- quick_test ----
    registry["quick_test"] = {
        "budget_group": "verify",
        "schema": auto_schema(QuickTestArgs),
        "risky": context.sandbox_backend is not None,
        "execution_tier": "linux_sandbox" if context.sandbox_backend is not None else TIER_HOST,
        "description": "环内快检 pytest nodeid/path。参数: nodeid 或 path, timeout",
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

    return registry


def legal_tool_names(registry: dict) -> set[str]:
    """返回注册表中所有可调用工具名的集合。"""
    return set(registry.keys())
