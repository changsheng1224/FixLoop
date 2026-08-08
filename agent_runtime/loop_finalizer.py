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
        _feedback_recalled_memories(agent, ts)
        if report_token.get("prompt_budget") is None:
            report_token["prompt_budget"] = getattr(agent.config, "prompt_budget", 0)
        context_summary = loop._build_context_summary()
        from agent_runtime.repair_runtime import tool_observation_summary

        report_body = {
            "run_id": ts.run_id,
            "agent": agent_name,
            "tool_steps": ts.tool_steps,
            "attempts": ts.attempts,
            "stop_reason": ts.stop_reason,
            "status": ts.status,
            "stream": {
                "enabled": bool(getattr(loop, "_stream_enabled", False)),
                "events": int(getattr(loop, "_stream_seq", 0)),
            },
            "phase": getattr(ts, "phase", ""),
            "turn": getattr(ts, "turn", 0),
            "failure_attribution": getattr(ts, "failure_attribution", {}),
            "runtime_contract": dict(getattr(ts, "runtime_contract", {}) or {}),
            "terminal_contract": {
                "terminal": str(getattr(ts, "status", ""))
                in {"completed", "failed", "stopped"},
                "stop_reason": str(getattr(ts, "stop_reason", "") or ""),
                "unresolved_side_effects": [
                    item
                    for item in (getattr(ts, "side_effects", []) or [])
                    if str(item.get("status", "")) in {"uncertain", "dispatched"}
                ],
            },
            "provider_capabilities": (
                getattr(agent.provider_capabilities, "__dict__", {})
                if hasattr(agent, "provider_capabilities")
                else {}
            ),
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
                "llm_call_limit": int(
                    getattr(agent.config, "max_llm_calls_per_repair", 0) or 0
                ),
                "repair_budget": loop._repair_budget.summary(),
                "budget_manager": loop._budget_manager.summary(),
                "tool_observations": tool_observation_summary(
                    agent.session.get("tool_observations", [])
                ),
                "last_tool_observation": agent.session.get("_last_tool_observation", {}),
            },
            "retry_summary": {
                "parse_retries": loop._retry_count,
                "model_attempts": ts.attempts,
                "tool_steps": ts.tool_steps,
            },
            "quota_usage": (agent.quota.quota_summary() if getattr(agent, "quota", None) else {}),
            "plan_todos": list(loop._plan_todos),
            "memory_health": loop._build_memory_health(),
            "memory_feedback": dict(agent.session.get("memory_feedback", {}) or {}),
            "config_snapshot": (
                agent.config.snapshot() if hasattr(agent.config, "snapshot") else {}
            ),
            "memory_usage_events": len(
                agent.session.get("memory", {}).get("memory_usage_events", [])
            ),
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
            trigger = "user_cancel" if ts.stop_reason == StopReason.USER_CANCEL.value else "ask_end"
            cp = create_checkpoint(
                agent,
                ts,
                ts.user_request,
                trigger=trigger,
                in_flight_tool=(
                    str(ts.node_timings.get("in_flight_tool", "") or "")
                    if trigger == "user_cancel"
                    else ""
                ),
            )
            ts.checkpoint_id = cp.get("checkpoint_id", "") if cp else ""
            ts.checkpoint_sequence = int(cp.get("sequence", 0) or 0) if cp else 0
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
        SessionStore(root=agent._cwd, trace=loop._emit).save(agent.session)
    except Exception:
        pass


def _promote_memory_candidates(agent, ts) -> None:
    """Promote candidate memories after ask() finalization."""
    try:
        from agent_runtime.features.memory.candidate import candidates_from_answer
        from agent_runtime.features.memory.governance import MemoryGovernanceService

        candidates = list(agent.session.get("_memory_candidates", []))
        candidates.extend(candidates_from_answer(ts.final_answer, ts.user_request))
        if candidates:
            memory_state = agent.session.setdefault("memory", {})
            identity = memory_state.get("memory_identity") or {}
            governance = MemoryGovernanceService(
                memory_state,
                repo_root=agent._cwd,
                user_id=str(identity.get("user_id", "") or ""),
                task_id=str(identity.get("task_id", "") or ""),
            )
            for candidate in candidates:
                governed = governance.ingest(
                    candidate,
                    scope=getattr(candidate, "scope", "task"),
                    repo_fingerprint=getattr(candidate, "repo_fingerprint", ""),
                )
                refs = list(getattr(candidate, "evidence_refs", []) or [])
                if refs:
                    governance.bind_evidence(governed.memory_id, refs)
            governance.run()
        agent.session.pop("_memory_candidates", None)
    except Exception:
        pass


def _feedback_recalled_memories(agent, ts) -> None:
    """Feed governed memories back using conservative run attribution.

    A verification/environment failure is inconclusive: it must not punish
    every recalled memory.  A successful repair supports recalled memories
    only when the run reached a successful terminal state.
    """
    try:
        from agent_runtime.features.memory.governance import MemoryGovernanceService

        memory_state = agent.session.get("memory") or {}
        identity = memory_state.get("memory_identity") or {}
        ids = list(dict.fromkeys(memory_state.get("recalled_memory_ids") or []))
        if not ids:
            return
        governance = MemoryGovernanceService(
            memory_state,
            repo_root=agent._cwd,
            user_id=str(identity.get("user_id", "") or ""),
            task_id=str(identity.get("task_id", "") or ""),
        )
        status = str(getattr(ts, "status", "") or "").lower()
        succeeded = status in {"success", "completed", "fixed", "passed"}
        attribution = memory_state.get("memory_context_attribution") or {}
        usage_events = memory_state.get("memory_usage_events", [])
        refs = []
        evidence = memory_state.get("working", {}).get("evidence_ledger", [])
        refs.extend(str(item.get("id", "")) for item in evidence if item.get("id"))
        refs.extend(str(item) for item in memory_state.get("recalled_observation_ids", []) if item)
        for memory_id in ids:
            applied = [
                event
                for event in usage_events
                if event.get("memory_id") == str(memory_id)
                and event.get("usage") in {"applied", "verified"}
            ]
            outcome = "supported" if succeeded and applied else "inconclusive"
            event_refs = [
                ref for event in applied for ref in event.get("evidence_refs", [])
            ]
            governance.record_usage(
                str(memory_id),
                outcome=outcome,
                evidence_refs=list(dict.fromkeys([*refs, *event_refs])),
                task_id=str(identity.get("task_id", "") or ""),
                trace_id=str(getattr(ts, "run_id", "") or ""),
                stage="final_verification",
                turn_id=str(attribution.get("turn_id", "") or ""),
                prompt_id=str(attribution.get("prompt_id", "") or ""),
                context_item_id=str(memory_id),
                cited=bool(applied),
                decision_reason="run_success_with_tool_application"
                if outcome == "supported"
                else "not_applied_or_inconclusive",
            )
        memory_state["memory_feedback"] = {
            "run_id": str(getattr(ts, "run_id", "") or ""),
            "outcome": "supported" if succeeded else "inconclusive",
            "memory_ids": ids,
        }
    except Exception:
        # Memory feedback must never make run finalization fail.
        return
