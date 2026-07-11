"""一次 repair case 的共享 trace 与按 Agent token 汇总。"""

from __future__ import annotations

from agent_runtime.run_ids import new_run_id
from agent_runtime.run_store import RunStore

# Repair trace 可选事件（Orchestrator / Agent 写入）
REPAIR_TRACE_EVENTS = frozenset(
    {
        "repair_started",
        "skill_hint_rendered",
        "skill_matched",
        "phase_timeout",
        "agent_ask_started",
        "agent_ask_finished",
        "blackboard_written",
        "blackboard_merged",
        "blackboard_merge_for_patch",
        "blackboard_prefix_subscribed",
        "blackboard_snapshot",
        "blackboard_conflicts",
    }
)


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

    def begin(self, issue: str, **extra: str) -> str:
        self.run_id = new_run_id()
        self.store.start_run_by_id(self.run_id)
        payload = {"issue_preview": issue[:300]}
        payload.update({k: v for k, v in extra.items() if v})
        self.emit(
            "orchestrator",
            "repair_started",
            payload,
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
        data.setdefault("run_id", self.run_id)
        self.store.append_trace_event(self.run_id, event, data)

    def write_agent_token(self, agent_name: str, usage: dict, extra: dict | None = None) -> None:
        """写入/累加单个 Agent 的 token 摘要（如 Patcher complete_once）。"""
        import json

        from src.repair.agent_report_loader import merge_agent_report

        run_dir = self.store.runs_dir / self.run_id
        existing_path = run_dir / f"agent_report.{agent_name}.json"
        existing: dict = {}
        if existing_path.is_file():
            try:
                existing = json.loads(existing_path.read_text(encoding="utf-8"))
            except Exception:
                existing = {}

        body = {
            "agent": agent_name,
            "run_id": self.run_id,
            "total_tokens": int(usage.get("total_tokens", 0) or 0),
            "input_tokens": int(usage.get("input_tokens", 0) or 0),
            "output_tokens": int(usage.get("output_tokens", 0) or 0),
            "api_calls": int(usage.get("api_calls", 0) or 0),
            "tool_steps": int((extra or {}).get("tool_steps", 0) or 0),
            "token_usage": usage.get("sections") or usage.get("token_usage") or {},
        }
        if extra:
            body.update(extra)
        body = merge_agent_report(existing, body)
        self.store.write_agent_report(self.run_id, agent_name, body)

    def finalize(self, state, token_summary: dict) -> None:
        from src.eval.token_usage import summarize_agent_tool_usage
        from src.repair.agent_report_loader import load_agent_reports_from_run, project_token_usage_by_agent
        from src.repair.rejection_aggregate import (
            aggregate_rejection_from_agent_reports,
        )
        from agent_runtime.tool_rejection import build_rejection_observability_payload
        from src.repair.l2_binding import L2_BINDING_SCHEMA_VERSION
        from src.repair.blackboard_merge import BLACKBOARD_SCHEMA_VERSION
        from src.repair.prompt_router import repair_plan_intent_snapshot
        from src.repair.timing_schema import phases_for_report
        from src.repair.ttft_aggregate import aggregate_ttft_from_agent_reports

        run_dir = self.store.runs_dir / self.run_id
        reports = load_agent_reports_from_run(run_dir)
        by_agent = project_token_usage_by_agent(reports)
        tool_summary = summarize_agent_tool_usage(by_agent)
        rejection_summary = aggregate_rejection_from_agent_reports(reports)
        ttft_summary = aggregate_ttft_from_agent_reports(reports)
        report = {
            "run_id": self.run_id,
            "status": state.status,
            "failure_tags": list(state.failure_tags),
            "phases": phases_for_report(state.node_timings),
            "l2_binding_schema_version": L2_BINDING_SCHEMA_VERSION,
            "agent_asks": [ref.to_dict() for ref in state.agent_asks],
            "blackboard_schema_version": BLACKBOARD_SCHEMA_VERSION,
            "blackboard": state.blackboard_snapshot or {},
            "token_usage_by_agent": by_agent,
            **tool_summary,
            **token_summary,
            **rejection_summary,
            **ttft_summary,
        }
        if state.repair_plan is not None:
            report["repair_plan"] = repair_plan_intent_snapshot(state.repair_plan)
        self.store.write_report_by_id(self.run_id, report)
        finished_payload = {
            "status": state.status,
            "failure_tags": list(state.failure_tags),
            "total_tokens": token_summary.get("total_tokens", 0),
            "total_tool_steps": tool_summary.get("total_tool_steps", 0),
            "tool_usage_by_agent": tool_summary.get("tool_usage_by_agent", {}),
            "agents": list(by_agent.keys()),
            "agent_asks": [ref.to_dict() for ref in state.agent_asks],
        }
        if state.repair_plan is not None:
            finished_payload["intent"] = repair_plan_intent_snapshot(state.repair_plan)
        obs = build_rejection_observability_payload(rejection_summary)
        if obs:
            finished_payload.update(obs)
        self.emit(
            "orchestrator",
            "repair_finished",
            finished_payload,
        )
