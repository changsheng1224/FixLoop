"""TaskState：单次 ask() 的运行状态 dataclass。

状态机：running → completed / stopped / failed
"""

import uuid
from dataclasses import dataclass


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
    resume_status: str = ""  # no-checkpoint | full-valid | partial-stale | workspace-mismatch

    @classmethod
    def create(cls, task_id: str = "", user_request: str = "") -> "TaskState":
        """创建新的 TaskState。

        Args:
            task_id: 任务标识符（可选，不传自动生成）。
            user_request: 用户输入。

        Returns:
            初始状态为 "running" 的 TaskState 实例。
        """
        return cls(
            run_id=str(uuid.uuid4())[:8],
            task_id=task_id or str(uuid.uuid4())[:8],
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
        )
