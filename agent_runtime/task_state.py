"""TaskState：单次 ask() 的运行状态 dataclass。

状态机：running → completed / stopped / failed
"""

from dataclasses import dataclass, field

from agent_runtime.run_ids import new_run_id


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
        """通用停机方法。"""
        self.stop_reason = reason
        self.status = status

    def stop_step_limit(self, max_steps: int):
        """步数耗尽。"""
        self.stop(f"tool_steps > {max_steps}", "stopped")

    def stop_retry_limit(self, max_attempts: int):
        """格式错误过多。"""
        self.stop(f"attempts >= {max_attempts}", "failed")

    def finish_success(self, final_answer: str):
        """正常结束。"""
        self.final_answer = final_answer
        self.status = "completed"
        self.stop_reason = "final"

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
        }

    @classmethod
    def from_dict(cls, data: dict) -> "TaskState":
        """从 dict 恢复 TaskState。"""
        return cls(
            run_id=data.get("run_id", ""),
            task_id=data.get("task_id", ""),
            user_request=data.get("user_request", ""),
            status=data.get("status", "running"),
            tool_steps=data.get("tool_steps", 0),
            attempts=data.get("attempts", 0),
            last_tool=data.get("last_tool", ""),
            stop_reason=data.get("stop_reason", ""),
            final_answer=data.get("final_answer", ""),
            checkpoint_id=data.get("checkpoint_id", ""),
            resume_status=data.get("resume_status", ""),
            node_timings=data.get("node_timings", {}),
        )
