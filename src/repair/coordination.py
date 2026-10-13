from __future__ import annotations

from agent_runtime.logging_setup import get_logger
from src.state import (
    RepairState,
)

log = get_logger("orchestrator")


class RepairCoordination:
    def __init__(self, session):
        self.session = session

    def set_collaboration_context(self, state: RepairState) -> None:
        """Pass current mode/phase to gateways when the runtime owns the state."""
        harness = getattr(self.session.repair_ctx, "harness_control", None)
        control_mode = getattr(getattr(harness, "control_mode", None), "value", "") or (
            state.human_control or {}
        ).get("mode", "auto")
        approved_tools = set((state.human_control or {}).get("approved_tools", []) or [])
        for agent in self.session.active_agents:
            gateway = getattr(agent, "tool_gateway", None)
            if gateway is not None and hasattr(gateway, "set_context"):
                gateway.set_context(
                    getattr(agent, "agent_name", "") or "agent",
                    mode=str(state.intent.get("mode", "repair")),
                    phase=str(state.phase),
                    evidence=bool(state.evidence or state.suspect_locations),
                    read_before_write=bool(state.control.allowed_edit or state.suspect_locations),
                    control_mode=control_mode,
                    approved_tools=approved_tools,
                )

    def on_collaboration_phase(self, state: RepairState, phase: str, reason: str = "") -> None:
        """Synchronize policy context and role set at a runtime phase boundary."""
        from src.collaboration_governance import CollaborationGovernance, RoleLifecycle

        transition = state.transition_phase(phase, reason, allow_recovery=phase == "recovery")
        if not transition.valid:
            state.node_timings.setdefault("phase_transition_rejections", []).append(
                transition.to_dict()
            )
            return
        harness = getattr(self.session.repair_ctx, "harness_control", None)
        if harness is not None:
            harness.record_phase(
                phase, state_revision=int(getattr(state, "state_revision", 0) or 0), reason=reason
            )
            if phase == "patch" and int(getattr(state, "retry_count", 0) or 0) >= harness.attempt:
                harness.retry(reason="repair retry")
        if phase == "patch":
            for role in ("verifier",):
                if role in state.active_roles:
                    event = CollaborationGovernance.lifecycle_transition(
                        state, role, RoleLifecycle.SUSPENDED, reason or "patch in progress"
                    )
                    self.emit_collaboration_event(event)
            role = "patcher"
            if getattr(self.session, role, None) is not None:
                event = CollaborationGovernance.lifecycle_transition(
                    state, role, RoleLifecycle.REACTIVATED, reason or "patch phase"
                )
                self.emit_collaboration_event(event)
        elif phase == "verify":
            if (
                getattr(self.session, "patcher", None) is not None
                and "patcher" in state.active_roles
            ):
                event = CollaborationGovernance.lifecycle_transition(
                    state, "patcher", RoleLifecycle.SUSPENDED, reason or "verification phase"
                )
                self.emit_collaboration_event(event)
            if getattr(self.session, "verifier", None) is not None:
                event = CollaborationGovernance.lifecycle_transition(
                    state, "verifier", RoleLifecycle.REACTIVATED, reason or "verification phase"
                )
                self.emit_collaboration_event(event)
        elif phase in {"done", "failed"}:
            for role in list(state.role_lifecycle):
                event = CollaborationGovernance.lifecycle_transition(
                    state, role, RoleLifecycle.COMPLETED, reason or "repair finalized"
                )
                self.emit_collaboration_event(event)
        runtime = getattr(self.session, "collaboration_runtime", None)
        if runtime is not None:
            try:
                runtime.advance(
                    phase, state, terminal_status=str(getattr(state, "status", "") or "")
                )
            except Exception as exc:
                state.agent_errors.setdefault("collaboration_runtime", str(exc)[:500])
        self.set_collaboration_context(state)

    def emit_collaboration_event(self, event: dict) -> None:
        ctx = getattr(self.session, "repair_ctx", None)
        tracer = getattr(ctx, "repair_tracer", None) if ctx else None
        if tracer is not None:
            tracer.emit("orchestrator", "agent_set_changed", event)

    def on_harness_event(self, state: RepairState, event: dict) -> None:
        """Persist one Harness event in state and the canonical repair trace."""
        events = list(getattr(state, "harness_events", []) or [])
        events.append(dict(event))
        state.harness_events = events[-2000:]
        control = getattr(self.session.repair_ctx, "harness_control", None)
        if control is not None:
            snapshot = control.snapshot()
            state.harness_control = {
                key: value for key, value in snapshot.items() if key not in {"events", "metrics"}
            }
            state.harness_metrics = dict(snapshot.get("metrics") or {})
            state.human_control = {
                **dict(getattr(state, "human_control", {}) or {}),
                "mode": snapshot.get("control_mode", "auto"),
            }
        tracer = getattr(self.session.repair_ctx, "repair_tracer", None)
        if tracer is not None:
            tracer.emit("harness", "harness_event", event, status=event.get("status") or None)
