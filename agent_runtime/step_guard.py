"""StepGuard：步进健康监控 — stall 终止 + 目标漂移检测。

在 AgentLoop 每步工具执行后评估步进健康度，检测两种异常：

1. **Stall（停滞）**：连续 K 步无 ``affected_paths``（文件无变更）
   → ``stop_reason=stall`` · task_summary 锚定 · replan 提示

2. **Goal Drift（目标漂移）**：连续 M 步操作的文件不在任务 suspect 范围内
   → 渐进式：2 步 emit ``goal_drift`` warning · 3 步 ``stop_reason=goal_drift``

Usage::

    guard = StepGuard()
    guard.reset(task_summary="修复 pricing.py 的除零错误")
    for each tool step:
        verdict = guard.evaluate(StepContext(
            tool_name=name, tool_args=args,
            has_affected=(len(affected_paths) > 0),
        ))
        if verdict is not None:
            # terminate loop: ts.stop_with_reason(verdict.reason, ...)
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field

from agent_runtime.stop_reasons import StopReason

# 默认阈值
DEFAULT_STALL_THRESHOLD = 3
DEFAULT_DRIFT_WARN = 2
DEFAULT_DRIFT_TERMINATE = 3

# 参与漂移检测的文件操作工具
_FILE_TOOLS = frozenset(
    {
        "read_file",
        "write_file",
        "patch_file",
        "ast_parse",
        "inspect_file",
    }
)

_READ_TOOLS = frozenset(
    {
        "read_file",
        "list_files",
        "search",
        "grep",
        "ast_parse",
        "inspect_file",
        "find_test",
        "git_blame",
        "git_diff",
        "java_ast_parse",
        "stack_parse",
        "java_stack_parse",
        "expand_observation",
    }
)
_WRITE_TOOLS = frozenset({"write_file", "patch_file", "apply_patch"})
DEFAULT_READS_BEFORE_CONVERGE = 6


def _extract_filenames(text: str) -> set[str]:
    """从文本中提取 .py 文件名（含路径片段）。"""
    if not text:
        return set()
    # 匹配 foo.py 或 path/to/foo.py
    matches = re.findall(r"[\w/\-]+\.py", text)
    return {m.split("/")[-1].split("\\")[-1] for m in matches}


def _tool_target_file(tool_name: str, tool_args: dict) -> str | None:
    """从工具参数中提取目标文件名。"""
    if tool_name not in _FILE_TOOLS:
        return None
    path = tool_args.get("path", "")
    if path:
        return path.replace("\\", "/").split("/")[-1]
    return None


@dataclass
class StepContext:
    """单步上下文：guard.evaluate() 的输入。"""

    tool_name: str = ""
    tool_args: dict = field(default_factory=dict)
    has_affected: bool = False
    progress_key: str = ""


@dataclass
class StepVerdict:
    """检测判决：非 None 表示应终止循环。"""

    reason: str  # StopReason 值
    detail: str
    replan_hint: str = ""
    action: str = "terminate"


class StepGuard:
    """步进健康监控器。

    每步工具执行后调用 evaluate()，返回 None（继续）或 StepVerdict（终止）。
    """

    def __init__(
        self,
        stall_threshold: int = DEFAULT_STALL_THRESHOLD,
        drift_warn: int = DEFAULT_DRIFT_WARN,
        drift_terminate: int = DEFAULT_DRIFT_TERMINATE,
        reads_before_converge: int = DEFAULT_READS_BEFORE_CONVERGE,
    ):
        self._stall_threshold = stall_threshold
        self._drift_warn = drift_warn
        self._drift_terminate = drift_terminate
        self._reads_before_converge = reads_before_converge

        self._stall_count = 0
        self._drift_count = 0
        self._suspect_files: set[str] = set()
        self._task_summary = ""
        self._drift_warned = False
        self._phase = "explore"
        self._read_keys: set[str] = set()
        self._read_ranges: dict[str, list[tuple[int, int]]] = {}
        self._reads_since_write = 0
        self._targeted_read_used = False
        self._convergence_novel_reads = 0
        self._localization_mode = False
        self._localization_window_closed = False
        self._convergence_reason = ""

    # ---- 公开 API ----

    def reset(
        self,
        task_summary: str = "",
        suspect_files: set[str] | None = None,
        *,
        localization_mode: bool = False,
    ) -> None:
        """重置计数器，注入当前任务上下文。

        Args:
            task_summary: 任务摘要（用于终止消息锚定）。
            suspect_files: 疑似文件集（用于漂移检测）。None 时从 task_summary 提取。
        """
        self._stall_count = 0
        self._drift_count = 0
        self._drift_warned = False
        self._task_summary = task_summary
        self._phase = "explore"
        self._read_keys.clear()
        self._read_ranges.clear()
        self._reads_since_write = 0
        self._targeted_read_used = False
        self._convergence_novel_reads = 0
        self._localization_mode = bool(localization_mode)
        self._localization_window_closed = False
        self._convergence_reason = ""
        if suspect_files is not None:
            self._suspect_files = set(suspect_files)
        else:
            self._suspect_files = _extract_filenames(task_summary)

    def evaluate(self, ctx: StepContext) -> StepVerdict | None:
        """评估当前步，返回判决或 None。

        调用顺序：先检查 stall，再检查 drift。首个命中即返回。
        """
        novel_evidence = bool(ctx.progress_key and ctx.progress_key not in self._read_keys)
        result = self._evaluate_stall(ctx, novel_evidence=novel_evidence)
        if result is not None:
            return result
        progress_result = self._record_progress(ctx, novel_evidence=novel_evidence)
        drift_result = self._evaluate_drift(ctx)
        return drift_result or progress_result

    def preflight(
        self,
        tool_name: str,
        tool_args: dict,
        *,
        read_reservation: dict | None = None,
    ) -> StepVerdict | None:
        """Gate repeated reads and require a patch decision after exploration."""
        if tool_name not in _READ_TOOLS:
            return None
        # A successful lock expansion creates a narrowly scoped capability.  It
        # must run before duplicate/convergence checks so the subsequent read can
        # refresh EditLock evidence for the new lock generation.
        if (
            tool_name == "read_file"
            and isinstance(read_reservation, dict)
            and read_reservation.get("kind") == "post_lock"
            and read_reservation.get("path")
            == str(tool_args.get("path") or "").replace("\\", "/")
        ):
            return StepVerdict(
                reason="",
                detail="扩锁后精确路径读取",
                action="allow_reserved_read",
            )
        if self._is_duplicate_read(tool_name, tool_args):
            if self._localization_mode and self._phase == "converge":
                self._localization_window_closed = True
            self.enter_convergence("duplicate_read")
            return StepVerdict(
                reason="",
                detail="读取范围与已有证据重复",
                action="block_duplicate_read",
                replan_hint="使用已有证据生成补丁，或只读取一个尚未覆盖的精确范围。",
            )
        if self._phase == "converge":
            if self._localization_mode and self._convergence_novel_reads < 3:
                self._convergence_novel_reads += 1
                return StepVerdict(
                    reason="",
                    detail="定位阶段允许新的证据读取",
                    action="allow_localization_read",
                )
            if not self._targeted_read_used:
                self._targeted_read_used = True
                return StepVerdict(
                    reason="",
                    detail="收敛阶段一次性定向读取",
                    action="allow_targeted_read",
                )
            return StepVerdict(
                reason="",
                detail="收敛阶段的定向读取额度已使用",
                action="block_convergence_read",
                replan_hint="现在应 apply_patch，或明确终止为需要更多上下文。",
            )
        return None

    def enter_convergence(self, reason: str) -> bool:
        """Enter convergence once; return True only for the transition."""
        if self._phase != "explore":
            return False
        self._phase = "converge"
        self._convergence_reason = reason
        return True

    @property
    def stall_count(self) -> int:
        """当前连续停滞步数。"""
        return self._stall_count

    @property
    def drift_count(self) -> int:
        """当前连续漂移步数。"""
        return self._drift_count

    @property
    def suspect_files(self) -> set[str]:
        """当前疑似文件集。"""
        return set(self._suspect_files)

    @property
    def phase(self) -> str:
        return self._phase

    @property
    def localization_mode(self) -> bool:
        """Whether the active agent owns implementation localization."""
        return self._localization_mode

    @property
    def localization_reads_available(self) -> bool:
        """Bounded novel-read budget retained after convergence for Patcher."""
        return (
            self._localization_mode
            and self._phase == "converge"
            and not self._localization_window_closed
            and self._convergence_novel_reads < 3
        )

    @property
    def convergence_reason(self) -> str:
        return self._convergence_reason

    @property
    def reads_since_write(self) -> int:
        return self._reads_since_write

    @property
    def targeted_read_available(self) -> bool:
        return self._phase == "converge" and not self._targeted_read_used

    def request_targeted_reread(self, reason: str = "") -> bool:
        """Open one bounded reread after a stale write precondition."""
        # A stale write can be discovered after the normal convergence read
        # was already consumed.  It is a new recovery event, so reopen exactly
        # one targeted read instead of falling back to the same stale write.
        if self._targeted_read_used and reason not in {"stale_preimage", "no_change"}:
            return False
        self._targeted_read_used = False
        self._phase = "converge"
        self._convergence_reason = reason or "targeted_reread"
        return True

    # ---- 内部检测器 ----

    def _evaluate_stall(
        self, ctx: StepContext, *, novel_evidence: bool = False
    ) -> StepVerdict | None:
        """StallDetector：连续 K 步无 affected_paths → 终止。"""
        if ctx.has_affected or novel_evidence:
            self._stall_count = 0
            return None
        self._stall_count += 1
        if self._stall_count >= self._stall_threshold:
            task = self._task_summary or "未知任务"
            return StepVerdict(
                reason=StopReason.STALL.value,
                detail=f"连续 {self._stall_count} 步无文件变更",
                replan_hint=(
                    f"任务「{task}」已停滞 {self._stall_count} 步。"
                    "建议：缩小排查范围、提供更具体的错误信息，"
                    "或 /reset 后重新描述问题。"
                ),
                action="replan_then_terminate",
            )
        return None

    def _record_progress(self, ctx: StepContext, *, novel_evidence: bool) -> StepVerdict | None:
        if ctx.tool_name in _WRITE_TOOLS and ctx.has_affected:
            self._phase = "patch"
            self._reads_since_write = 0
            return None
        if ctx.tool_name not in _READ_TOOLS or not ctx.progress_key:
            return None
        if novel_evidence:
            self._read_keys.add(ctx.progress_key)
            self._remember_read_range(ctx.tool_name, ctx.tool_args)
        self._reads_since_write += 1
        if self._phase == "explore" and self._reads_since_write >= self._reads_before_converge:
            self.enter_convergence("read_limit_without_write")
            return StepVerdict(
                reason="",
                detail=f"已读取 {self._reads_since_write} 次但尚未修改实现",
                action="enter_convergence",
                replan_hint="仅再允许一次定向读取，随后必须写入或明确终止。",
            )
        return None

    @staticmethod
    def read_progress_key(tool_name: str, tool_args: dict) -> str:
        if tool_name not in _READ_TOOLS:
            return ""
        normalized = {str(k): tool_args[k] for k in sorted(tool_args)}
        if "path" in normalized:
            normalized["path"] = str(normalized["path"] or ".").replace("\\", "/")
        return f"{tool_name}:{json.dumps(normalized, sort_keys=True, default=str)}"

    def _is_duplicate_read(self, tool_name: str, tool_args: dict) -> bool:
        key = self.read_progress_key(tool_name, tool_args)
        if key in self._read_keys:
            return True
        if tool_name != "read_file":
            return False
        path = str(tool_args.get("path") or "").replace("\\", "/")
        if not path:
            return False
        start = max(1, int(tool_args.get("start", 1) or 1))
        end = max(start, int(tool_args.get("end", 200) or 200))
        for old_start, old_end in self._read_ranges.get(path, []):
            overlap = max(0, min(end, old_end) - max(start, old_start) + 1)
            shorter = min(end - start + 1, old_end - old_start + 1)
            if shorter > 0 and overlap / shorter >= 0.7:
                return True
        return False

    def _remember_read_range(self, tool_name: str, tool_args: dict) -> None:
        if tool_name != "read_file":
            return
        path = str(tool_args.get("path") or "").replace("\\", "/")
        if not path:
            return
        start = max(1, int(tool_args.get("start", 1) or 1))
        end = max(start, int(tool_args.get("end", 200) or 200))
        self._read_ranges.setdefault(path, []).append((start, end))

    def _evaluate_drift(self, ctx: StepContext) -> StepVerdict | None:
        """DriftDetector：连续 M 步操作无关文件 → 渐进式响应。"""
        target = _tool_target_file(ctx.tool_name, ctx.tool_args)
        if target is None:
            # 非文件操作工具（如 search/grep/run_shell）：不影响 drift 计数
            return None
        if not self._suspect_files:
            # 无 suspect 信息时无法判断，跳过
            return None

        is_related = target in self._suspect_files
        if is_related:
            self._drift_count = 0
            self._drift_warned = False
            return None

        self._drift_count += 1
        if self._drift_count >= self._drift_terminate:
            task = self._task_summary or "未知任务"
            suspects = ", ".join(sorted(self._suspect_files)[:5]) or "无"
            return StepVerdict(
                reason=StopReason.GOAL_DRIFT.value,
                detail=(
                    f"连续 {self._drift_count} 步操作与任务无关的文件"
                    f"（目标: {suspects}，当前: {target}）"
                ),
                action="replan_then_terminate",
                replan_hint=(
                    f"任务「{task}」疑似目标漂移。"
                    f"当前操作文件 {target!r} 不在 suspect 列表 [{suspects}] 中。"
                    "建议：确认排查范围是否正确，或 /reset 后提供更完整的堆栈信息。"
                ),
            )
        if self._drift_count >= self._drift_warn and not self._drift_warned:
            self._drift_warned = True
            # 返回 warning 级别的判决（reason=None 表示仅 warning，不终止）
            # 调用方通过 reason 是否为空判断是 warning 还是 terminate
            return StepVerdict(
                reason="",  # 空 reason = warning，不终止
                detail=f"目标漂移预警：{target!r} 不在 suspect 列表中",
                replan_hint="",
                action="warn",
            )
        return None
