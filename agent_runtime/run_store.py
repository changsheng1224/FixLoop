"""Run Store：运行工件持久化到 .agent/runs/<run_id>/。

每个 run 目录含：
    task_state.json — 运行状态（原子写）
    trace.jsonl     — 逐事件时间线（JSONL 追加，>阈值自动 gzip）
    report.json     — 运行摘要（原子写）

trace 保留策略：默认 30 天 TTL（FIXLOOP_RUN_TTL_DAYS 可配，0=禁用）。
start_run() 时自动清理过期目录。
"""

import gzip
import json
import os
import shutil
import time
from datetime import UTC, datetime
from pathlib import Path

# trace.jsonl 超此行数自动 gzip 归档（FIXLOOP_TRACE_GZIP_LINES 可配，0=禁用）
_DEFAULT_TRACE_GZIP_LINES = 1000

# run 目录默认保留天数（FIXLOOP_RUN_TTL_DAYS 可覆盖，0=禁用自动清理）
_DEFAULT_RUN_TTL_DAYS = 30


def _trace_gzip_threshold() -> int:
    val = os.environ.get("FIXLOOP_TRACE_GZIP_LINES", str(_DEFAULT_TRACE_GZIP_LINES))
    try:
        return int(val)
    except ValueError:
        return _DEFAULT_TRACE_GZIP_LINES


def read_trace_path(path: Path) -> list[str]:
    """透明读取 trace 文件（支持 .jsonl 和 .jsonl.gz）。

    Args:
        path: trace.jsonl 路径（优先），不存在则尝试 trace.jsonl.gz。

    Returns:
        trace 每行文本列表，文件不存在返回空列表。
    """
    if not path.exists():
        gz = path.with_suffix(".jsonl.gz")
        if gz.is_file():
            with gzip.open(gz, "rt", encoding="utf-8") as f:
                return [line.rstrip("\n") for line in f]
        return []
    if path.suffix == ".gz":
        with gzip.open(path, "rt", encoding="utf-8") as f:
            return [line.rstrip("\n") for line in f]
    return path.read_text(encoding="utf-8").strip().splitlines()


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

    @property
    def ttl_days(self) -> int:
        """run 目录保留天数（FIXLOOP_RUN_TTL_DAYS 环境变量，默认 30，0=禁用）。"""
        val = os.environ.get("FIXLOOP_RUN_TTL_DAYS", str(_DEFAULT_RUN_TTL_DAYS))
        try:
            return int(val)
        except ValueError:
            return _DEFAULT_RUN_TTL_DAYS

    def cleanup_older_than(self, days: int | None = None) -> int:
        """删除超过指定天数的旧 run 目录。

        Args:
            days: 保留天数。None 使用 ttl_days。

        Returns:
            删除的目录数量。
        """
        days = days if days is not None else self.ttl_days
        if days <= 0:
            return 0
        if not self.runs_dir.is_dir():
            return 0

        cutoff = time.time() - days * 86400
        deleted = 0
        for run_dir in self.runs_dir.iterdir():
            if not run_dir.is_dir():
                continue
            try:
                if run_dir.stat().st_mtime < cutoff:
                    shutil.rmtree(run_dir)
                    deleted += 1
            except OSError:
                pass
        return deleted

    def start_run(self, task_state) -> Path:
        """创建 run 目录（自动清理过期 runs）。

        Args:
            task_state: TaskState 实例（用于获取 run_id）。

        Returns:
            run 目录路径。
        """
        return self.start_run_by_id(task_state.run_id)

    def start_run_by_id(self, run_id: str) -> Path:
        """按 run_id 创建 run 目录（自动清理过期 runs）。"""
        self.cleanup_older_than()
        run_dir = self.runs_dir / run_id
        run_dir.mkdir(parents=True, exist_ok=True)
        return run_dir

    def write_task_state(self, task_state) -> Path:
        """写入 task_state.json（原子写）。

        FIXLOOP_ENCRYPT_KEY 设置时加密 user_request / final_answer。
        """
        from agent_runtime.crypto_utils import encrypt, is_encryption_enabled

        run_dir = self.start_run(task_state)
        path = run_dir / "task_state.json"
        data = task_state.to_dict()
        if is_encryption_enabled():
            if data.get("user_request"):
                data["user_request"] = encrypt(data["user_request"])
            if data.get("final_answer"):
                data["final_answer"] = encrypt(data["final_answer"])
            data["_encrypted"] = True
        tmp = path.with_suffix(".tmp")
        tmp.write_text(
            json.dumps(data, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        tmp.replace(path)
        return path

    def append_trace(self, task_state, event: str, payload: dict | None = None):
        """追加一行 JSONL 追踪事件（经脱敏）。"""
        self.append_trace_event(task_state.run_id, event, payload)

    def append_trace_event(
        self,
        run_id: str,
        event: str,
        payload: dict | None = None,
        *,
        status: str | None = None,
    ):
        """按 run_id 追加 trace 事件（多 Agent 共享 trace 时使用）。

        schema_version=1：写入 Canonical 信封；enrich 失败时降级为旧三字段。
        """
        from agent_runtime.security import redact_artifact

        run_dir = self.start_run_by_id(run_id)
        path = run_dir / "trace.jsonl"
        created_at = datetime.now(UTC).isoformat()
        safe_payload = redact_artifact(payload) if payload else None
        try:
            from agent_runtime.canonical_trace import enrich_record

            record = enrich_record(
                run_id=run_id,
                event=event,
                created_at=created_at,
                payload=safe_payload,
                status=status,
            )
        except Exception:
            record = {
                "event": event,
                "created_at": created_at,
            }
            if safe_payload:
                record["payload"] = safe_payload
        line = json.dumps(record, ensure_ascii=False, default=str)
        with open(path, "a", encoding="utf-8") as f:
            f.write(line + "\n")
        # Prometheus / Langfuse 导出：失败不得影响 JSONL 主路径
        try:
            from agent_runtime.observability import after_trace_append

            after_trace_append(record)
        except Exception:
            pass

    def load_trace_events(self, run_id: str) -> list[dict]:
        """读取 run 的 trace 事件列表（支持 .jsonl.gz）。"""
        path = self.runs_dir / run_id / "trace.jsonl"
        events: list[dict] = []
        for line in read_trace_path(path):
            if not line.strip():
                continue
            try:
                events.append(json.loads(line))
            except json.JSONDecodeError:
                continue
        return events

    def load_ordered_trace(self, run_id: str) -> list[dict]:
        """按 timestamp + seq 还原执行顺序。"""
        from agent_runtime.canonical_trace import order_events

        return order_events(self.load_trace_events(run_id))

    def validate_trace(self, run_id: str, *, require_terminal: bool = True) -> list[str]:
        """Return integrity issues for one persisted run trace."""
        from agent_runtime.canonical_trace import validate_runtime_trace

        return validate_runtime_trace(
            self.load_ordered_trace(run_id),
            require_terminal=require_terminal,
        )

    def write_task_state_named(self, run_id: str, filename: str, task_state) -> Path:
        """写入命名 task_state 文件（共享 run 下每个 Agent 一份）。"""
        run_dir = self.start_run_by_id(run_id)
        path = run_dir / filename
        tmp = path.with_suffix(".tmp")
        tmp.write_text(
            json.dumps(task_state.to_dict(), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        tmp.replace(path)
        return path

    def write_agent_report(self, run_id: str, agent_name: str, report: dict) -> Path:
        """写入单个 Agent 的 token/运行摘要（共享 run 模式）。"""
        from agent_runtime.security import redact_artifact

        run_dir = self.start_run_by_id(run_id)
        path = run_dir / f"agent_report.{agent_name}.json"
        report = redact_artifact(report)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(
            json.dumps(report, ensure_ascii=False, indent=2, default=str),
            encoding="utf-8",
        )
        tmp.replace(path)
        return path

    def write_report_by_id(self, run_id: str, report: dict) -> Path:
        """按 run_id 写入 report.json（原子写，经脱敏）。"""
        from agent_runtime.security import redact_artifact

        run_dir = self.start_run_by_id(run_id)
        path = run_dir / "report.json"
        report = redact_artifact(report)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(
            json.dumps(report, ensure_ascii=False, indent=2, default=str),
            encoding="utf-8",
        )
        tmp.replace(path)
        return path

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

    # ── trace 透明读取 + gzip 归档 ──

    def _trace_path(self, run_id: str) -> Path:
        """返回 trace 实际路径（.jsonl 优先，.jsonl.gz fallback）。"""
        run_dir = self.runs_dir / run_id
        plain = run_dir / "trace.jsonl"
        if plain.is_file():
            return plain
        gz = run_dir / "trace.jsonl.gz"
        if gz.is_file():
            return gz
        return plain  # 新 run 尚未创建时返回默认路径

    def read_trace_lines(self, run_id: str) -> list[str]:
        """透明读取 trace 行（自动处理 gzip）。"""
        return read_trace_path(self._trace_path(run_id))

    def compress_trace_if_needed(self, run_id: str) -> dict | None:
        """trace.jsonl 超阈值时 gzip 归档。

        Args:
            run_id: 运行 ID。

        Returns:
            压缩统计 dict 或 None（未触发压缩时）。
        """
        threshold = _trace_gzip_threshold()
        if threshold <= 0:
            return None

        run_dir = self.runs_dir / run_id
        plain = run_dir / "trace.jsonl"
        if not plain.is_file():
            return None

        lines = plain.read_text(encoding="utf-8").count("\n")
        if lines < threshold:
            return None

        original_bytes = plain.stat().st_size
        gz = run_dir / "trace.jsonl.gz"
        with open(plain, "rb") as src:
            with gzip.open(gz, "wb") as dst:
                dst.writelines(src)
        compressed_bytes = gz.stat().st_size
        plain.unlink()

        return {
            "original_bytes": original_bytes,
            "compressed_bytes": compressed_bytes,
            "compression_ratio": round(compressed_bytes / max(original_bytes, 1), 3),
            "trace_lines": lines,
        }
