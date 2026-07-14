"""AgentLoop run finalization and artifact persistence."""

from agent_runtime.model_timing import build_report_latency_fields
from agent_runtime.stop_reasons import StopReason


def finalize_agent_run(loop, ts) -> None:
    """Persist final run artifacts for an AgentLoop instance."""
    from agent_runtime.checkpoint import create_checkpoint
    from agent_runtime.features.memory import promote_durable_memory
    from agent_runtime.session_store import SessionStore

    try:
        agent = loop.agent
        store = loop._get_store()
        shared = getattr(agent, "shared_run_id", None)
        agent_name = getattr(agent, "_agent_name", "") or "agent"
        session_usage = getattr(agent.model_client, "session_usage", None) or {}
        from agent_runtime.token_accounting import build_report_token_fields

        report_token = build_report_token_fields(session_usage, loop._last_token_meta)
        report_latency = build_report_latency_fields(loop._call_timings)
        agent._last_run_node_timings = dict(ts.node_timings)
        agent._last_call_timings = list(loop._call_timings)
        if report_token.get("prompt_budget") is None:
            report_token["prompt_budget"] = getattr(agent.config, "prompt_budget", 0)
        context_summary = loop._build_context_summary()
        report_body = {
            "run_id": ts.run_id,
            "agent": agent_name,
            "tool_steps": ts.tool_steps,
            "attempts": ts.attempts,
            "stop_reason": ts.stop_reason,
            "status": ts.status,
            "prompt_cache_key": getattr(agent._prefix, "hash", ""),
            "node_timings": ts.node_timings,
            "tier_summary": {
                "host_calls": loop._tier_counts.get("host", 0),
                "container_calls": loop._tier_counts.get("container", 0),
                "host_tools": loop._tier_tools.get("host", {}),
                "container_tools": loop._tier_tools.get("container", {}),
            },
            "context_summary": context_summary,
            "runtime_metrics": {
                "parse_retry_count": loop._retry_count,
                "tool_steps": ts.tool_steps,
                "cache_hit_rate": context_summary.get("cache_hit_rate", 0.0),
                "llm_calls": loop._llm_call_count,
                "llm_call_limit": int(getattr(agent.config, "max_llm_calls_per_repair", 0) or 0),
            },
            "retry_summary": {
                "parse_retries": loop._retry_count,
                "model_attempts": ts.attempts,
                "tool_steps": ts.tool_steps,
            },
            "quota_usage": (
                agent.quota.quota_summary()
                if getattr(agent, "quota", None)
                else {}
            ),
            "plan_todos": list(loop._plan_todos),
            "memory_health": loop._build_memory_health(),
            **report_token,
            **report_latency,
            **ts.rejection_report_fields(),
        }
        if ts.l2_agent:
            report_body.update(
                {
                    "task_id": ts.task_id,
                    "l2_repair_run_id": ts.l2_repair_run_id,
                    "l2_agent": ts.l2_agent,
                    "l2_phase": ts.l2_phase,
                    "l2_attempt": ts.l2_attempt,
                }
            )
        agent._last_task_state = ts
        if shared:
            store.write_task_state_named(shared, f"task_state.{agent_name}.json", ts)
            store.write_agent_report(shared, agent_name, report_body)
        else:
            trigger = (
                "user_cancel"
                if ts.stop_reason == StopReason.USER_CANCEL.value
                else "ask_end"
            )
            cp = create_checkpoint(agent, ts, ts.user_request, trigger=trigger)
            ts.checkpoint_id = cp.get("run_id", "") if cp else ""
            store.write_task_state(ts)
            compress_stats = store.compress_trace_if_needed(ts.run_id)
            if compress_stats:
                report_body["trace_compressed"] = True
                report_body["trace_compression"] = compress_stats
            store.write_report(ts, report_body)
        promote_durable_memory(
            ts.user_request,
            ts.final_answer,
            root=agent._cwd,
        )
        _promote_memory_candidates(agent, ts)
        SessionStore(root=agent._cwd).save(agent.session)
    except Exception:
        pass


def _promote_memory_candidates(agent, ts) -> None:
    """Promote candidate memories after ask() finalization."""
    try:
        from agent_runtime.features.memory.candidate import (
            candidates_from_answer,
            promote_candidates,
        )
        from agent_runtime.features.memory.durable import DurableMemoryStore

        store = DurableMemoryStore(agent._cwd)
        candidates = list(agent.session.get("_memory_candidates", []))
        candidates.extend(candidates_from_answer(ts.final_answer, ts.user_request))
        if candidates:
            light_client = getattr(agent, "light_client", None)
            promote_candidates(store, candidates, light_client=light_client)
        agent.session.pop("_memory_candidates", None)
    except Exception:
        pass
