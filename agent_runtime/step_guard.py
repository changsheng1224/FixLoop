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

import re
from dataclasses import dataclass, field

from agent_runtime.stop_reasons import StopReason

# 默认阈值
DEFAULT_STALL_THRESHOLD = 3
DEFAULT_DRIFT_WARN = 2
DEFAULT_DRIFT_TERMINATE = 3

# 参与漂移检测的文件操作工具
_FILE_TOOLS = frozenset({
    "read_file", "write_file", "patch_file",
    "ast_parse", "inspect_file",
})


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


@dataclass
class StepVerdict:
    """检测判决：非 None 表示应终止循环。"""

    reason: str  # StopReason 值
    detail: str
    replan_hint: str = ""


class StepGuard:
    """步进健康监控器。

    每步工具执行后调用 evaluate()，返回 None（继续）或 StepVerdict（终止）。
    """

    def __init__(
        self,
        stall_threshold: int = DEFAULT_STALL_THRESHOLD,
        drift_warn: int = DEFAULT_DRIFT_WARN,
        drift_terminate: int = DEFAULT_DRIFT_TERMINATE,
    ):
        self._stall_threshold = stall_threshold
        self._drift_warn = drift_warn
        self._drift_terminate = drift_terminate

        self._stall_count = 0
        self._drift_count = 0
        self._suspect_files: set[str] = set()
        self._task_summary = ""
        self._drift_warned = False

    # ---- 公开 API ----

    def reset(self, task_summary: str = "", suspect_files: set[str] | None = None) -> None:
        """重置计数器，注入当前任务上下文。

        Args:
            task_summary: 任务摘要（用于终止消息锚定）。
            suspect_files: 疑似文件集（用于漂移检测）。None 时从 task_summary 提取。
        """
        self._stall_count = 0
        self._drift_count = 0
        self._drift_warned = False
        self._task_summary = task_summary
        if suspect_files is not None:
            self._suspect_files = set(suspect_files)
        else:
            self._suspect_files = _extract_filenames(task_summary)

    def evaluate(self, ctx: StepContext) -> StepVerdict | None:
        """评估当前步，返回判决或 None。

        调用顺序：先检查 stall，再检查 drift。首个命中即返回。
        """
        result = self._evaluate_stall(ctx)
        if result is not None:
            return result
        return self._evaluate_drift(ctx)

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

    # ---- 内部检测器 ----

    def _evaluate_stall(self, ctx: StepContext) -> StepVerdict | None:
        """StallDetector：连续 K 步无 affected_paths → 终止。"""
        if ctx.has_affected:
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
            )
        return None

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
            )
        return None
