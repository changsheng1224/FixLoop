from __future__ import annotations

from agent_runtime.logging_setup import get_logger
from src.repair.verification.service import VerificationService
from src.repair.verification.verify import (
    record_verify_timings,
)
from src.state import (
    RepairState,
)

log = get_logger("orchestrator")

DEFAULT_REPAIR_TIMEOUT_S = 180


class VerificationController:
    def __init__(self, session):
        self.session = session

    def block_unconfirmed_verification(self, state, reason):
        from src.repair.recovery_outcome import publish_recovery_outcome

        service = getattr(self.session, "verify_service", None)
        if service is not None:
            service.cleanup_confirmed = False
        state.control.coordination_status = "recovery_required"
        state.set_status("recovery_required", "verification_cleanup_unconfirmed")
        state.agent_errors["verification_cleanup"] = reason
        context = getattr(self.session.patcher, "tool_context", None)
        if context is not None:
            context.execution_uncertain = True
        binding = getattr(self.session, "plan_binding", None)
        coordinator = getattr(binding, "coordinator", None) or getattr(
            self.session, "entry_coordinator", None
        )
        if coordinator is not None and coordinator.lease is not None:
            current = coordinator.store.snapshot(coordinator.lease.run_id)
            if current.owner_token == coordinator.lease.owner_token:
                coordinator.store.mark_recovery_required(
                    coordinator.lease, error_code="verification_cleanup_unconfirmed"
                )
        publish_recovery_outcome(
            state,
            {
                "run_id": state.repair_run_id,
                "status": "recovery_required",
                "resources": [
                    {
                        "resource_id": "host_verification",
                        "kind": "test_process",
                        "status": "unknown",
                        "cleanup": "unverified",
                        "error_code": "verification_cleanup_unconfirmed",
                    }
                ],
            },
            stage="cancel",
            reason_code="verification_cleanup_unconfirmed",
            emitter=self.session.events.progress_emitter(),
        )

    def verification_service(self):
        if getattr(self.session, "verify_service", None) is None:
            self.session.verify_service = VerificationService(
                repo_root=self.session.repo_root,
                policy=self.session.execution_policy,
                sandbox_context=self.session.sandbox_context,
                test_target=lambda state: (
                    (self.session.request.verification_target if self.session.request else "")
                    or self.pick_test_path(state)
                ),
                cancel_token=lambda: (
                    self.session.repair_ctx.cancel_token if self.session.repair_ctx else None
                ),
            )
        return self.session.verify_service

    def run_verifier(self, state):
        if self.session.plan_binding is not None:
            return self.session.plan_binding.run_verifier(lambda: self.run_verifier_impl(state))
        return self.run_verifier_impl(state)

    def run_verifier_impl(self, state):
        from src.collaboration.isolation import role_projection

        state.node_timings["verifier_input_projection"] = role_projection(state, "verifier")
        run = self.verification_service().run(state)
        minimum = self.session.request.verification_min_tests if self.session.request else 1
        if run.result.execution_status != "passed" or run.result.total_tests < minimum:
            run.result.all_passed = False
        record_verify_timings(state, run)
        return run.result

    def record_validation_exit(self, state, key):
        run = self.verification_service().run(state)
        code = run.internal.get("exit_code", run.internal.get("pytest_exit_code"))
        if run.internal.get("actual_tier") == "static":
            code = None
        setattr(state.control, key, code)
        state.node_timings[key + "_category"] = run.internal.get("category", "unknown")
        state.node_timings[key + "_receipt_id"] = run.internal.get("receipt_id", "")
        state.node_timings[key + "_receipt"] = dict(run.internal)

    def pick_test_path(self, state: RepairState) -> str:
        """Prefer actual failure targets, then public repository test references."""
        from src.repair.verification.fail_surface import preferred_verify_targets
        from src.repair.verification.test_references import normalize_related_test_refs

        candidates = list(preferred_verify_targets(state))
        context = state.retrieved_context
        if context is not None:
            for item in context.related_tests or []:
                if isinstance(item, str):
                    candidates.append(item)
                elif isinstance(item, dict):
                    for key in ("nodeid", "name", "path"):
                        if value := item.get(key):
                            candidates.append(str(value))
                            break
        if getattr(self.session, "sandbox_context", None) is not None:
            from agent_runtime.linux_sandbox.command_profile import test_target

            targets = []
            for candidate in candidates:
                try:
                    targets.append(test_target(candidate))
                except ValueError:
                    continue
        else:
            targets = normalize_related_test_refs(candidates, self.session.repo_root or None)
        return targets[0] if targets else ""

    def verification_enabled(self) -> bool:
        return self.session.execution_policy.enabled
