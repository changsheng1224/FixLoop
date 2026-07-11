"""TaskState：单次 ask() 的运行状态 dataclass。

状态机：running → completed / stopped / failed
"""

from dataclasses import dataclass, field

from agent_runtime.run_ids import new_run_id
from agent_runtime.stop_reasons import (
    StopReason,
    normalize_stop_reason,
    stop_reason_detail_from_legacy,
)


@dataclass
class TaskState:
    """单次 ask() 的运行状态。

    追踪工具步数、模型尝试次数、停机原因和最终答案。
    """

    run_id: str
    task_id: str
    user_request: str
    status: str = "running"  # running | completed | stopped | failed
    tool_steps: int = 0
    attempts: int = 0
    last_tool: str = ""
    stop_reason: str = ""
    final_answer: str = ""
    checkpoint_id: str = ""
    resume_status: str = ""
    node_timings: dict = field(default_factory=dict)  # 如 {"prompt_build_ms": 5, ...}
    tool_rejections_by_layer: dict = field(default_factory=dict)
    tool_rejections_by_gate: dict = field(default_factory=dict)
    permission_denied_by_tool: dict = field(default_factory=dict)
    l2_repair_run_id: str = ""
    l2_agent: str = ""
    l2_phase: str = ""
    l2_attempt: int = 0

    @classmethod
    def create(
        cls,
        task_id: str = "",
        user_request: str = "",
        run_id: str | None = None,
    ) -> "TaskState":
        """创建新的 TaskState。

        Args:
            task_id: 任务标识符（可选，不传自动生成）。
            user_request: 用户输入。
            run_id: 共享 run 目录 ID（多 Agent repair 时由 Orchestrator 注入）。

        Returns:
            初始状态为 "running" 的 TaskState 实例。
        """
        rid = run_id or new_run_id()
        return cls(
            run_id=rid,
            task_id=task_id or rid,
            user_request=user_request,
        )

    # ---- 状态转换方法 ----

    def record_attempt(self):
        """每次调模型后 +1。"""
        self.attempts += 1

    def record_tool(self, name: str):
        """每次执行工具后 +1，记录最后使用的工具名。"""
        self.tool_steps += 1
        self.last_tool = name

    def record_tool_rejection(self, tool_name: str, metadata: dict | None):
        """累计工具拒绝语义（Gateway / Executor 双层）。"""
        if not metadata:
            return
        status = metadata.get("tool_status")
        if status not in ("rejected", "error"):
            return

        layer = metadata.get("rejection_layer")
        code = metadata.get("tool_error_code")
        if layer == "gateway" or code == "permission_denied":
            self._incr(self.tool_rejections_by_layer, "gateway")
            self._incr(self.tool_rejections_by_gate, "gateway")
            if code == "permission_denied":
                self._incr(self.permission_denied_by_tool, tool_name)
            return

        if layer != "executor":
            return

        self._incr(self.tool_rejections_by_layer, "executor")
        gate_id = metadata.get("gate_id")
        if gate_id is not None:
            self._incr(self.tool_rejections_by_gate, str(gate_id))

    @staticmethod
    def _incr(mapping: dict, key: str, delta: int = 1) -> None:
        mapping[key] = mapping.get(key, 0) + delta

    def rejection_report_fields(self) -> dict:
        """report.json 拒绝统计字段（无拒绝时返回空 dict）。"""
        if not (
            self.tool_rejections_by_layer
            or self.tool_rejections_by_gate
            or self.permission_denied_by_tool
        ):
            return {}
        return {
            "tool_rejections_by_layer": dict(self.tool_rejections_by_layer),
            "tool_rejections_by_gate": dict(self.tool_rejections_by_gate),
            "permission_denied_by_tool": dict(self.permission_denied_by_tool),
        }

    def stop(self, reason: str, status: str):
        """通用停机（legacy 字符串会归一化为 canonical）。"""
        self.stop_with_reason(
            normalize_stop_reason(reason),
            status,
            detail=stop_reason_detail_from_legacy(reason),
        )

    def stop_with_reason(
        self,
        reason: StopReason | str,
        status: str,
        *,
        detail: str = "",
    ):
        """写入 canonical stop_reason，可选 detail 进 node_timings。"""
        self.stop_reason = str(reason)
        self.status = status
        if detail:
            self.node_timings["stop_reason_detail"] = detail

    def stop_step_limit(self, max_steps: int):
        """步数耗尽。"""
        self.stop_with_reason(
            StopReason.STEP_LIMIT,
            "stopped",
            detail=f"tool_steps > {max_steps}",
        )

    def stop_retry_limit(self, max_attempts: int):
        """格式错误过多。"""
        self.stop_with_reason(
            StopReason.PARSE_FAIL,
            "failed",
            detail=f"attempts >= {max_attempts}",
        )

    def stop_step_timeout(self, timeout_s: int, step: int):
        """单步 wall-clock 超时。"""
        self.stop_with_reason(StopReason.STEP_TIMEOUT, "stopped")
        self.node_timings["step_timeout_s"] = int(timeout_s)
        self.node_timings["step_timeout_step"] = int(step)

    def stop_user_cancel(self, *, in_flight: str = "", phase: str = ""):
        """用户协作式取消。"""
        if in_flight:
            self.node_timings["in_flight_tool"] = in_flight
        if phase:
            self.node_timings["cancel_phase"] = phase
        self.stop_with_reason(StopReason.USER_CANCEL, "stopped")

    def finish_success(self, final_answer: str):
        """正常结束。"""
        self.final_answer = final_answer
        self.status = "completed"
        self.stop_reason = StopReason.FINAL.value

    # ---- 序列化 ----

    def to_dict(self) -> dict:
        """序列化为 dict。"""
        return {
            "run_id": self.run_id,
            "task_id": self.task_id,
            "user_request": self.user_request,
            "status": self.status,
            "tool_steps": self.tool_steps,
            "attempts": self.attempts,
            "last_tool": self.last_tool,
            "stop_reason": self.stop_reason,
            "final_answer": self.final_answer,
            "checkpoint_id": self.checkpoint_id,
            "resume_status": self.resume_status,
            "node_timings": self.node_timings,
            "l2_repair_run_id": self.l2_repair_run_id,
            "l2_agent": self.l2_agent,
            "l2_phase": self.l2_phase,
            "l2_attempt": self.l2_attempt,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "TaskState":
        """从 dict 恢复 TaskState。"""
        raw_reason = data.get("stop_reason", "")
        node_timings = dict(data.get("node_timings", {}))
        detail = stop_reason_detail_from_legacy(raw_reason)
        if detail and "stop_reason_detail" not in node_timings:
            node_timings["stop_reason_detail"] = detail
        return cls(
            run_id=data.get("run_id", ""),
            task_id=data.get("task_id", ""),
            user_request=data.get("user_request", ""),
            status=data.get("status", "running"),
            tool_steps=data.get("tool_steps", 0),
            attempts=data.get("attempts", 0),
            last_tool=data.get("last_tool", ""),
            stop_reason=normalize_stop_reason(raw_reason),
            final_answer=data.get("final_answer", ""),
            checkpoint_id=data.get("checkpoint_id", ""),
            resume_status=data.get("resume_status", ""),
            node_timings=node_timings,
            tool_rejections_by_layer=dict(data.get("tool_rejections_by_layer", {})),
            tool_rejections_by_gate=dict(data.get("tool_rejections_by_gate", {})),
            permission_denied_by_tool=dict(data.get("permission_denied_by_tool", {})),
            l2_repair_run_id=data.get("l2_repair_run_id", ""),
            l2_agent=data.get("l2_agent", ""),
            l2_phase=data.get("l2_phase", ""),
            l2_attempt=int(data.get("l2_attempt", 0) or 0),
        )
