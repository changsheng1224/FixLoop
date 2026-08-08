"""Governance primitives for multi-role repair collaboration.

Agents submit proposals and observations. Runtime owns lifecycle, policy and
state transitions; this module contains no case-specific repair decisions.
"""

from __future__ import annotations

import time
from copy import deepcopy
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any


class RoleLifecycle(StrEnum):
    DORMANT = "dormant"
    ACTIVE = "active"
    SUSPENDED = "suspended"
    COMPLETED = "completed"
    FAILED = "failed"
    REACTIVATED = "reactivated"


@dataclass(frozen=True)
class ToolPolicy:
    tool: str
    roles: frozenset[str] = frozenset()
    modes: frozenset[str] = frozenset({"repair"})
    phases: frozenset[str] = frozenset()
    requires_evidence: bool = False
    requires_read_before_write: bool = False
    side_effect: str = "read"
    risk_level: str = "low"
    requires_approval: bool = False


@dataclass
class RoleDecision:
    allowed: bool
    reason: str = ""
    alternatives: list[str] = field(default_factory=list)


def apply_state_patch(state, patch: dict[str, Any], *, actor: str, expected_revision: int) -> dict:
    """Apply an owner-scoped CAS update to shared repair state.

    Agents may propose state changes, but the runtime remains the single
    writer for fields it owns.  Unknown fields and stale revisions are
    rejected instead of being silently merged.
    """
    if int(expected_revision) != int(getattr(state, "state_revision", 0)):
        return {
            "accepted": False,
            "status": "stale",
            "reason": "state_revision_mismatch",
            "expected_revision": int(expected_revision),
            "current_revision": int(state.state_revision),
        }
    owners = getattr(state, "field_owners", {}) or {}
    unknown = [key for key in patch if not hasattr(state, key)]
    if unknown:
        return {
            "accepted": False,
            "status": "rejected",
            "reason": "unknown_fields",
            "fields": unknown,
        }
    unauthorized = [key for key in patch if owners.get(key, actor) != actor]
    if unauthorized:
        return {
            "accepted": False,
            "status": "rejected",
            "reason": "field_owner_mismatch",
            "fields": unauthorized,
        }
    before = deepcopy(getattr(state, "__dict__", {}))
    for key, value in patch.items():
        setattr(state, key, value)
    validator = getattr(state, "validate_invariants", None)
    if callable(validator):
        try:
            validator(strict=True)
        except (TypeError, ValueError) as exc:
            state.__dict__.clear()
            state.__dict__.update(before)
            return {
                "accepted": False,
                "status": "rejected",
                "reason": "state_invariant_violation",
                "detail": str(exc),
                "fields": list(patch),
            }
    state.state_revision += 1
    return {
        "accepted": True,
        "status": "accepted",
        "revision": state.state_revision,
        "fields": list(patch),
    }


def atomic_collaboration_update(
    state,
    board,
    patch: dict[str, Any],
    *,
    actor: str,
    expected_revision: int,
    writes: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Atomically apply a state patch and Blackboard writes.

    The in-process transaction is deliberately small: it provides rollback
    semantics for the runtime's state + board pair without introducing a
    database dependency.  Callers receive a structured rejection result.
    """
    state_before = deepcopy(getattr(state, "__dict__", {}))
    board_before = board.snapshot()
    result = apply_state_patch(
        state, patch, actor=actor, expected_revision=expected_revision
    )
    if not result.get("accepted"):
        return result
    for write in writes or []:
        if not board.write(
            write["key"],
            write.get("value"),
            source_agent=str(write.get("source_agent") or actor),
            ttl=write.get("ttl"),
            expected_revision=write.get("expected_revision"),
        ):
            state.__dict__.clear()
            state.__dict__.update(state_before)
            board.restore_snapshot(board_before)
            return {
                "accepted": False,
                "status": "rejected",
                "reason": "blackboard_write_conflict",
                "key": write.get("key", ""),
            }
    return {**result, "blackboard_revision": board.revision}


def sanitize_skill_policy(
    suggested_tools: list[str],
    governance: CollaborationGovernance,
    *,
    role: str,
    mode: str = "repair",
    phase: str = "patch",
    trust_level: str = "verified",
) -> dict[str, Any]:
    """Reduce Skill hints to runtime-authorized guidance.

    Skills never grant permissions. Untrusted Skills can still provide
    guidance, but their tool hints are always intersected with the runtime
    policy.
    """
    allowed = [
        tool
        for tool in dict.fromkeys(suggested_tools or [])
        if governance.authorize(
            tool,
            role=role,
            mode=mode,
            phase=phase,
            evidence=True,
            read_before_write=True,
        ).allowed
    ]
    return {
        "suggested_tools": allowed,
        "tool_policy_source": "runtime_intersection",
        "guidance_only": str(trust_level).lower() == "untrusted",
    }


class CollaborationGovernance:
    """State and policy controller shared by Orchestrator and middleware."""

    def __init__(self, *, policies: list[ToolPolicy] | None = None, registry=None):
        if registry is not None:
            self.refresh_from_registry(registry)
            return
        self.policies = {p.tool: p for p in policies or default_tool_policies()}

    def refresh_from_registry(self, registry) -> None:
        """Rebuild authorization policy from the canonical ToolSpec registry."""
        policies = [
                ToolPolicy(
                    tool=spec.name,
                    roles=spec.roles,
                    modes=spec.modes,
                    phases=spec.phases,
                    requires_evidence=spec.requires_evidence,
                    requires_read_before_write=spec.requires_read_before_write,
                    side_effect=getattr(spec, "side_effect", "read"),
                    risk_level=getattr(spec, "risk_level", "low"),
                    requires_approval=bool(getattr(spec, "requires_approval", False)),
                )
                for name in registry.names()
                if (spec := registry.get(name)) is not None
                and spec.lifecycle in {"active", "experimental", "deprecated"}
            ]
        self.policies = {policy.tool: policy for policy in policies}

    def authorize(
        self,
        tool: str,
        *,
        role: str,
        mode: str = "repair",
        phase: str = "",
        evidence: bool = False,
        read_before_write: bool = True,
        control_mode: str = "auto",
        approved: bool = False,
    ) -> RoleDecision:
        policy = self.policies.get(tool)
        if policy is None:
            return RoleDecision(False, "unknown_tool")
        if policy.roles and "*" not in policy.roles and role not in policy.roles:
            return RoleDecision(False, "role_not_allowed", self._alternatives(role, mode, phase))
        if mode not in policy.modes:
            return RoleDecision(False, "mode_not_allowed", self._alternatives(role, mode, phase))
        if policy.phases and phase not in policy.phases:
            return RoleDecision(False, "phase_not_allowed", self._alternatives(role, mode, phase))
        if policy.requires_evidence and not evidence:
            return RoleDecision(False, "missing_evidence", self._alternatives(role, mode, phase))
        if policy.requires_read_before_write and not read_before_write:
            return RoleDecision(False, "file_not_read", self._alternatives(role, mode, phase))
        if str(control_mode) == "read_only" and policy.side_effect != "read":
            return RoleDecision(False, "human_read_only", self._alternatives(role, mode, phase))
        if (
            str(control_mode) == "approval_required"
            and policy.side_effect != "read"
            and policy.requires_approval
            and not approved
        ):
            return RoleDecision(
                False,
                "human_approval_required",
                self._alternatives(role, mode, phase),
            )
        return RoleDecision(True, "allowed")

    @staticmethod
    def authorize_handoff(handoff, state, *, expected_revision: int | None = None) -> RoleDecision:
        """Validate a typed handoff against active roles and state revision."""
        errors = handoff.validate()
        if errors:
            return RoleDecision(False, "handoff_invalid:" + ",".join(errors))
        if expected_revision is not None and int(expected_revision) != int(
            getattr(state, "state_revision", 0)
        ):
            return RoleDecision(False, "handoff_state_revision_stale")
        active = set(getattr(state, "active_roles", []) or [])
        if active and handoff.from_role not in active:
            return RoleDecision(False, "handoff_source_role_inactive")
        if active and handoff.to_role not in active:
            return RoleDecision(False, "handoff_target_role_inactive")
        return RoleDecision(True, "handoff_allowed")

    @staticmethod
    def record_handoff(state, handoff, *, status: str = "accepted") -> dict[str, Any]:
        """Append a handoff audit record to RepairState without sharing mutables."""
        from src.collaboration.contracts import HandoffStatus

        decision = CollaborationGovernance.authorize_handoff(handoff, state)
        if not decision.allowed:
            raise ValueError(decision.reason)
        handoff.status = HandoffStatus(str(status))
        payload = handoff.to_dict()
        state.handoffs.append(payload)
        state.handoffs = state.handoffs[-200:]
        state.state_revision += 1
        return payload

    @staticmethod
    def register_task(state, task) -> dict[str, Any]:
        errors = task.validate()
        if errors:
            raise ValueError("; ".join(errors))
        payload = task.to_dict()
        state.collaboration_tasks.append(payload)
        state.collaboration_tasks = state.collaboration_tasks[-500:]
        state.state_revision += 1
        return payload

    def _alternatives(self, role: str, mode: str, phase: str) -> list[str]:
        return [
            name
            for name, policy in self.policies.items()
            if (not policy.roles or "*" in policy.roles or role in policy.roles)
            and mode in policy.modes
            and (not policy.phases or phase in policy.phases)
        ][:6]

    @staticmethod
    def lifecycle_transition(state, role: str, status: RoleLifecycle, reason: str) -> dict:
        current = state.role_lifecycle.setdefault(role, {})
        previous = current.get("status", RoleLifecycle.DORMANT.value)
        current.update(
            {
                "status": status.value,
                "reason": reason,
                "at_revision": state.state_revision,
                "updated_at": time.time(),
            }
        )
        if status in {RoleLifecycle.ACTIVE, RoleLifecycle.REACTIVATED}:
            if role not in state.active_roles:
                state.active_roles.append(role)
        elif role in state.active_roles:
            state.active_roles.remove(role)
        state.state_revision += 1
        return {"role": role, "from": previous, "to": status.value, "reason": reason}

    @staticmethod
    def apply_state_projection(state, role: str) -> dict[str, Any]:
        """Return role-scoped state without exposing mutable global structures."""
        candidate_files = state.changed_files or (
            state.repair_plan.suspect_files if state.repair_plan else []
        )
        return {
            "schema_version": state.schema_version,
            "state_revision": state.state_revision,
            "run_id": state.repair_run_id,
            "role": role,
            "phase": state.phase,
            "status": state.status,
            "intent": dict(state.intent),
            "hypotheses": list(state.hypotheses),
            "evidence": list(state.evidence),
            "candidate_files": list(candidate_files),
            "verification": (
                state.verification_result.to_dict() if state.verification_result else {}
            ),
            "active_roles": list(state.active_roles),
            "collaboration_tasks": list(getattr(state, "collaboration_tasks", []) or []),
            "handoffs": list(getattr(state, "handoffs", []) or [])[-20:],
        }

    @staticmethod
    def collaboration_attribution(state) -> dict[str, Any]:
        conflicts = (state.blackboard_snapshot or {}).get("conflicts", [])
        stale = sum(1 for item in conflicts if item.get("status") == "stale")
        if stale:
            primary = "stale_state_conflict"
        elif conflicts:
            primary = "blackboard_conflict"
        else:
            primary = "unknown"
        return {
            "primary": primary,
            "confidence": 0.85 if conflicts else 0.2,
            "evidence": [{"kind": "blackboard", "count": len(conflicts), "stale": stale}],
        }


def default_tool_policies() -> list[ToolPolicy]:
    read = frozenset({"context", "localization", "patch", "verification"})
    return [
        ToolPolicy("read_file", frozenset({"patcher", "verifier", "baseline"}), phases=read),
        ToolPolicy("search", frozenset({"patcher", "verifier", "baseline"}), phases=read),
        ToolPolicy("grep", frozenset({"patcher", "verifier", "baseline"}), phases=read),
        ToolPolicy(
            "apply_patch",
            frozenset({"patcher"}),
            modes=frozenset({"repair", "refactor"}),
            phases=frozenset({"patch"}),
            requires_evidence=True,
            requires_read_before_write=True,
        ),
        ToolPolicy(
            "quick_test",
            frozenset({"patcher", "verifier"}),
            phases=frozenset({"verify", "verification", "patch"}),
        ),
    ]
