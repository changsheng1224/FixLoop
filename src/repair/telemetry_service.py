"""Trace and metrics lifecycle with explicit run context and agent inputs."""

from pathlib import Path

from src.state import RepairState


class RepairTelemetryService:
    def __init__(self, repo_root, context, patcher, agents, l1_key, harness_event_sink):
        self.repo_root = repo_root
        self.context = context
        self.patcher = patcher
        self.agents = agents
        self.l1_key = l1_key
        self.harness_event_sink = harness_event_sink

    def initialize(self, state: RepairState) -> None:
        from agent_runtime.log_context import bind_run_id
        from agent_runtime.tokenizers import resolve_tokenizer_spec
        from src.repair.run_trace import RepairRunTracer

        tracer = RepairRunTracer(
            self.repo_root,
            state_root=str(
                getattr(getattr(self.patcher, "tool_context", None), "state_root", "") or ""
            ),
        )
        l1_meta = {}
        if self.l1_key:
            l1_meta["l1_prompt_cache_key"] = self.l1_key
            ref = self.patcher
            prefix = getattr(ref, "_prefix", None) if ref is not None else None
            if prefix is not None:
                if prefix.tool_signature:
                    l1_meta["tool_signature"] = prefix.tool_signature
                if prefix.workspace_fingerprint:
                    l1_meta["workspace_fingerprint"] = prefix.workspace_fingerprint
        phase_cfg = self.context.phase_timeout_config if self.context else None
        if phase_cfg is not None and phase_cfg.any_enabled():
            l1_meta["phase_timeout_budgets"] = phase_cfg.budget_dict()
        run_id = tracer.begin(state.issue_input, run_id=state.repair_run_id, **l1_meta)
        from agent_runtime.harness_engineering import (
            ExecutionContract,
            HarnessControlPlane,
            build_harness_manifest,
            stable_fingerprint,
        )

        def _harness_sink(event: dict) -> None:
            self.harness_event_sink(state, event)

        harness = HarnessControlPlane(
            run_id,
            max_attempts=max(1, int(state.max_retries or 0) + 1),
            budget_limits={
                str(key): float(value)
                for key, value in (state.tool_budget or {}).items()
                if isinstance(value, int | float) and float(value) >= 0
            },
            event_sink=_harness_sink,
        )
        self.context.harness_control = harness
        model = ""
        provider = ""
        for agent in self.agents:
            config = getattr(agent, "config", None)
            if config is not None:
                model = model or str(getattr(config, "model", "") or "")
                provider = provider or str(getattr(config, "provider", "") or "")
        registry = getattr(self.patcher, "tool_registry", None)
        tool_names = registry.names() if registry is not None and hasattr(registry, "names") else ()
        manifest = build_harness_manifest(
            self.repo_root,
            run_id=run_id,
            model=model,
            provider=provider,
            tool_schema_hash=stable_fingerprint(tool_names),
            config={"max_retries": state.max_retries, "tool_budget": dict(state.tool_budget)},
        )
        state.harness_manifest = manifest
        contract = ExecutionContract(
            run_id=run_id,
            attempt=1,
            state_revision=int(getattr(state, "state_revision", 0) or 0),
            phase=str(getattr(state, "phase", "") or ""),
            budget={
                key: float(value)
                for key, value in (state.tool_budget or {}).items()
                if isinstance(value, int | float)
            },
            manifest_fingerprint=manifest["manifest_fingerprint"],
        )
        harness.start(contract)
        state.harness_control = {
            key: value
            for key, value in harness.snapshot().items()
            if key not in {"events", "metrics"}
        }
        state.harness_metrics = harness.metrics.snapshot()
        state.human_control = {
            **dict(getattr(state, "human_control", {}) or {}),
            "mode": harness.control_mode.value,
        }
        tracer.emit("harness", "harness_manifest", manifest, status="ok")
        tracer.bind_agents(*self.agents)
        if self.context is not None:
            self.context.repair_tracer = tracer
        state.repair_run_id = run_id
        tokenizer_by_agent: dict[str, dict] = {}
        for name, agent in (("patcher", self.patcher),):
            if agent is None:
                continue
            config = getattr(agent, "config", None)
            spec = resolve_tokenizer_spec(
                getattr(config, "model", ""),
                getattr(config, "provider", ""),
            )
            tokenizer_by_agent[name] = {
                "rule_id": spec.rule_id,
                "tokenizer_fallback": spec.fallback,
                "tokenizer_id": spec.tokenizer_id,
            }
        if tokenizer_by_agent:
            state.node_timings["tokenizer_by_agent"] = tokenizer_by_agent

        self.context.log_run_id_token = bind_run_id(run_id)

    def finalize(self, state):
        from agent_runtime.log_context import reset_run_id

        ctx = self.context
        tracer = ctx.repair_tracer if ctx is not None else None
        if tracer is None:
            return
        harness = getattr(ctx, "harness_control", None) if ctx is not None else None
        if harness is not None and not harness.terminal:
            from agent_runtime.harness_engineering import (
                BadCaseStore,
                HarnessFailureCode,
                HarnessStatus,
                attribute_harness_failure,
                extract_bad_case,
            )

            status = str(getattr(state, "status", "") or "").lower()
            if status == "fixed":
                target = HarnessStatus.COMPLETED
                attribution = None
            elif status in {"timeout", "timed_out"}:
                target = HarnessStatus.TIMED_OUT
                attribution = attribute_harness_failure(
                    code=HarnessFailureCode.TIMEOUT,
                    evidence_refs=[f"trace:{state.repair_run_id}"],
                )
            elif status == "user_cancel" or state.control.user_cancel:
                target = HarnessStatus.CANCELLED
                attribution = attribute_harness_failure(
                    code=HarnessFailureCode.CANCELLED,
                    evidence_refs=[f"trace:{state.repair_run_id}"],
                )
            else:
                target = HarnessStatus.FAILED
                attribution = attribute_harness_failure(
                    code=(
                        HarnessFailureCode.VERIFICATION_FAILED
                        if state.verification_result and not state.verification_result.all_passed
                        else HarnessFailureCode.UNKNOWN
                    ),
                    contributing_causes=list(state.failure_tags),
                    evidence_refs=[f"trace:{state.repair_run_id}"],
                )
            harness.finish(target, reason=status or target.value, attribution=attribution)
            snapshot = harness.snapshot()
            snapshot["manifest_fingerprint"] = state.harness_manifest.get(
                "manifest_fingerprint", ""
            )
            state.harness_control = {
                key: value for key, value in snapshot.items() if key not in {"events", "metrics"}
            }
            state.harness_events = list(snapshot.get("events") or [])
            state.harness_metrics = dict(snapshot.get("metrics") or {})
            state.harness_attribution = attribution.to_dict() if attribution else {}
            state.human_control = {
                **dict(getattr(state, "human_control", {}) or {}),
                "mode": snapshot.get("control_mode", "auto"),
            }
            if target != HarnessStatus.COMPLETED:
                record = extract_bad_case(snapshot)
                if record is not None:
                    state.bad_cases.append(record.to_dict())
                    try:
                        BadCaseStore(self.repo_root).append(record)
                    except OSError:
                        pass
        token_summary = state.node_timings.get("token_usage") or {}
        tracer.finalize(state, token_summary)
        tracer.unbind_agents(*self.agents)
        if ctx is not None:
            ctx.repair_tracer = None
        token = ctx.log_run_id_token if ctx is not None else None
        if token is not None:
            reset_run_id(token)
            if ctx is not None:
                ctx.log_run_id_token = None

    def _reset_token_tracking(self) -> None:
        from src.eval.token_usage import reset_clients_session_usage

        reset_clients_session_usage(*self.agents)

    def _attach_token_usage(self, state: RepairState) -> None:
        from src.eval.token_usage import build_repair_token_usage, resolve_model_clients

        clients = resolve_model_clients(*self.agents)
        if not clients:
            return
        summary = build_repair_token_usage(
            clients,
            Path(self.repo_root),
            since_ts=self.context.repair_started_at if self.context else None,
            repair_run_id=state.repair_run_id or None,
        )
        state.node_timings["total_tokens"] = summary["total_tokens"]
        state.node_timings["token_usage"] = summary
        if summary.get("token_usage_by_agent"):
            state.node_timings["token_usage_by_agent"] = summary["token_usage_by_agent"]
        if summary.get("tool_usage_by_agent"):
            state.node_timings["tool_usage_by_agent"] = summary["tool_usage_by_agent"]
        if summary.get("total_tool_steps") is not None:
            state.node_timings["total_tool_steps"] = summary["total_tool_steps"]
        # 分 Agent latency
        latency_by_agent: dict[str, dict] = {}
        for agent in self.agents:
            client = getattr(agent, "model_client", None)
            name = getattr(agent, "_agent_name", "") or "agent"
            if client and hasattr(client, "latency_stats"):
                latency_by_agent[name] = client.latency_stats()
        if latency_by_agent:
            state.node_timings["latency_by_agent"] = latency_by_agent

    def _attach_rejection_stats(self, state: RepairState) -> None:
        from src.repair.rejection_aggregate import (
            apply_gateway_denials_to_agent_errors,
            summarize_repair_rejections,
        )

        run_id = state.repair_run_id or ""
        if not run_id:
            return
        from agent_runtime.state_root import state_root_for

        run_dir = (
            state_root_for(
                self.repo_root,
                getattr(getattr(self.patcher, "tool_context", None), "state_root", ""),
            )
            / ".agent"
            / "runs"
            / run_id
        )
        summary = summarize_repair_rejections(run_dir)
        for key, value in summary.items():
            state.node_timings[key] = value
        apply_gateway_denials_to_agent_errors(
            state.agent_errors,
            summary.get("permission_denied_by_agent"),
        )
