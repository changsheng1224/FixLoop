"""一次 repair case 的共享 trace 与按 Agent token 汇总。"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from agent_runtime.run_store import RunStore


class RepairRunTracer:
    """Orchestrator 驱动：多 Agent 写入同一 trace.jsonl，结束时合并 report。"""

    def __init__(self, repo_root: str):
        self.repo_root = repo_root
        self.run_id = ""
        self._store: RunStore | None = None

    @property
    def store(self) -> RunStore:
        if self._store is None:
            self._store = RunStore(self.repo_root)
        return self._store

    def begin(self, issue: str) -> str:
        ts = datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
        self.run_id = f"repair-{ts}-{uuid.uuid4().hex[:6]}"
        self.store.start_run_by_id(self.run_id)
        self.emit(
            "orchestrator",
            "repair_started",
            {"issue_preview": issue[:300]},
        )
        return self.run_id

    def bind_agents(self, *agents) -> None:
        for agent in agents:
            if agent is not None:
                agent.shared_run_id = self.run_id

    def unbind_agents(self, *agents) -> None:
        for agent in agents:
            if agent is not None:
                agent.shared_run_id = None

    def emit(self, agent_name: str, event: str, payload: dict | None = None) -> None:
        data = dict(payload or {})
        data.setdefault("agent", agent_name)
        self.store.append_trace_event(self.run_id, event, data)

    def write_agent_token(self, agent_name: str, usage: dict, extra: dict | None = None) -> None:
        """写入/累加单个 Agent 的 token 摘要（如 Patcher complete_once）。"""
        import json

        run_dir = self.store.runs_dir / self.run_id
        existing_path = run_dir / f"agent_report.{agent_name}.json"
        total = int(usage.get("total_tokens", 0) or 0)
        inp = int(usage.get("input_tokens", 0) or 0)
        out = int(usage.get("output_tokens", 0) or 0)
        calls = int(usage.get("api_calls", 0) or 0)
        tool_steps = int((extra or {}).get("tool_steps", 0) or 0)
        if existing_path.is_file():
            try:
                old = json.loads(existing_path.read_text(encoding="utf-8"))
                total += int(old.get("total_tokens", 0) or 0)
                inp += int(old.get("input_tokens", 0) or 0)
                out += int(old.get("output_tokens", 0) or 0)
                calls += int(old.get("api_calls", 0) or 0)
                tool_steps += int(old.get("tool_steps", 0) or 0)
            except Exception:
                pass
        body = {
            "agent": agent_name,
            "run_id": self.run_id,
            "total_tokens": total,
            "input_tokens": inp,
            "output_tokens": out,
            "api_calls": calls,
            "tool_steps": tool_steps,
            "token_usage": usage.get("sections") or usage.get("token_usage") or {},
        }
        if extra:
            body.update(extra)
        self.store.write_agent_report(self.run_id, agent_name, body)

    def finalize(self, state, token_summary: dict) -> None:
        from src.eval.token_usage import collect_agent_reports_from_run, summarize_agent_tool_usage

        by_agent = collect_agent_reports_from_run(self.store.runs_dir / self.run_id)
        tool_summary = summarize_agent_tool_usage(by_agent)
        report = {
            "run_id": self.run_id,
            "status": state.status,
            "token_usage_by_agent": by_agent,
            **tool_summary,
            **token_summary,
        }
        self.store.write_report_by_id(self.run_id, report)
        self.emit(
            "orchestrator",
            "repair_finished",
            {
                "status": state.status,
                "total_tokens": token_summary.get("total_tokens", 0),
                "total_tool_steps": tool_summary.get("total_tool_steps", 0),
                "tool_usage_by_agent": tool_summary.get("tool_usage_by_agent", {}),
                "agents": list(by_agent.keys()),
            },
        )
