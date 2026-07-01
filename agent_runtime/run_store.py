"""Run Store：运行工件持久化到 .agent/runs/<run_id>/。

每个 run 目录含：
    task_state.json — 运行状态（原子写）
    trace.jsonl     — 逐事件时间线（JSONL 追加）
    report.json     — 运行摘要（原子写）
"""

import json
from datetime import UTC, datetime
from pathlib import Path


class RunStore:
    """运行工件持久化存储。

    目录结构：
        .agent/runs/{run_id}/
        ├── task_state.json
        ├── trace.jsonl
        └── report.json
    """

    def __init__(self, root: str):
        self.root = Path(root)
        self.runs_dir = self.root / ".agent" / "runs"

    def start_run(self, task_state) -> Path:
        """创建 run 目录。

        Args:
            task_state: TaskState 实例（用于获取 run_id）。

        Returns:
            run 目录路径。
        """
        run_dir = self.runs_dir / task_state.run_id
        run_dir.mkdir(parents=True, exist_ok=True)
        return run_dir

    def write_task_state(self, task_state) -> Path:
        """写入 task_state.json（原子写）。

        Args:
            task_state: TaskState 实例。

        Returns:
            写入的文件路径。
        """
        run_dir = self.start_run(task_state)
        path = run_dir / "task_state.json"
        tmp = path.with_suffix(".tmp")
        tmp.write_text(
            json.dumps(task_state.to_dict(), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        tmp.replace(path)
        return path

    def append_trace(self, task_state, event: str, payload: dict | None = None):
        """追加一行 JSONL 追踪事件（经脱敏）。"""
        from agent_runtime.security import redact_artifact

        run_dir = self.start_run(task_state)
        path = run_dir / "trace.jsonl"
        record = {
            "event": event,
            "created_at": datetime.now(UTC).isoformat(),
        }
        if payload:
            record["payload"] = redact_artifact(payload)
        line = json.dumps(record, ensure_ascii=False, default=str)
        with open(path, "a", encoding="utf-8") as f:
            f.write(line + "\n")

    def write_report(self, task_state, report: dict):
        """写入 report.json（原子写，经脱敏）。"""
        from agent_runtime.security import redact_artifact

        run_dir = self.start_run(task_state)
        path = run_dir / "report.json"
        report = redact_artifact(report)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(
            json.dumps(report, ensure_ascii=False, indent=2, default=str),
            encoding="utf-8",
        )
        tmp.replace(path)
