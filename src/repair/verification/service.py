"""One execution policy for baseline, patch verification and failure rechecks."""

from __future__ import annotations

import threading

from agent_runtime.cancellation import CancelledError
from agent_runtime.test_execution import ProcessCleanupUnconfirmedError
from src.repair.verification.verify import (
    BwrapVerifyStrategy,
    DockerVerifyStrategy,
    ProfileVerifyStrategy,
    PytestVerifyStrategy,
    StaticVerifyStrategy,
    VerifyRun,
    _verify_cancelled_run,
    record_verify_timings,
)
from src.state import VerificationResult


class VerificationService:
    """Resolve auto once; later phases cannot silently change execution layers."""

    def __init__(self, *, repo_root, policy, sandbox_context, test_target, cancel_token):
        self.repo_root = repo_root
        self.policy = policy
        self.sandbox_context = sandbox_context
        self.test_target = test_target
        self.cancel_token = cancel_token
        self.selected_tier = None
        self.cleanup_confirmed = True
        self.active = threading.Event()

    @property
    def requested_tier(self):
        return self.policy.requested_tier

    @property
    def mode(self):
        return "static" if (self.selected_tier or self.requested_tier) == "static" else "tests"

    def _unavailable(self, tier, language):
        return VerifyRun(
            VerificationResult(failure_logs=[f"{tier} verification unavailable for {language}"]),
            0,
            {
                "requested_tier": tier,
                "actual_tier": "none",
                "execution_tier": tier,
                "completed": False,
                "not_started": True,
                "cleanup": "confirmed",
                "category": "verification_environment_failed",
            },
            "verification_environment_failed",
        )

    def run(self, state):
        import time

        started = time.time()
        self.active.set()
        try:
            return self._run(state)
        except CancelledError:
            return _verify_cancelled_run(started)
        finally:
            self.active.clear()

    def _run(self, state):
        language = (state.repair_plan.language if state.repair_plan else "python") or "python"
        language = language.lower()
        token = self.cancel_token()
        target = self.test_target(state)
        tier = self.selected_tier or self.requested_tier
        if not self.cleanup_confirmed:
            raise ProcessCleanupUnconfirmedError("previous verification cleanup unconfirmed")
        if tier == "auto":
            tier = self.policy.initial_tier(language)
        try:
            if tier == "linux_sandbox":
                run = BwrapVerifyStrategy(self.sandbox_context).run(
                    self.repo_root, target, cancel_token=token, language=language
                )
            elif tier == "static":
                run = StaticVerifyStrategy().run(
                    self.repo_root, target, cancel_token=token, language=language
                )
            elif tier == "container":
                run = (
                    DockerVerifyStrategy().run(self.repo_root, target, cancel_token=token)
                    if language == "python"
                    else self._unavailable(tier, language)
                )
                if (
                    run.error == "sandbox_unavailable"
                    and self.selected_tier is None
                    and self.requested_tier == "auto"
                ):
                    fallback = (
                        "host"
                        if self.policy.host_fallback
                        else "static"
                        if self.policy.static_fallback
                        else None
                    )
                    if fallback:
                        self.selected_tier = fallback
                        return self._run(state)
            elif tier == "host":
                run = (
                    PytestVerifyStrategy().run(self.repo_root, target, cancel_token=token)
                    if language == "python"
                    else ProfileVerifyStrategy().run(
                        self.repo_root, target, cancel_token=token, language=language
                    )
                )
            else:
                run = self._unavailable(tier, language)
        except ProcessCleanupUnconfirmedError:
            self.cleanup_confirmed = False
            raise
        if run.internal.get("cleanup") in {"unverified", "unknown"}:
            self.cleanup_confirmed = False
            raise ProcessCleanupUnconfirmedError("verification process tree cleanup unconfirmed")
        # Pin even an unavailable layer: future phases must use the same policy.
        self.selected_tier = tier
        return run


class VerificationMixin:
    def _block_unconfirmed_verification(self, state, reason):
        from src.repair.recovery_outcome import publish_recovery_outcome

        service = getattr(self, "_verify_service", None)
        if service is not None:
            service.cleanup_confirmed = (
                False  # A later worker return cannot erase the recovery fence.
            )
        state.control.coordination_status = "recovery_required"
        state.set_status("recovery_required", "verification_cleanup_unconfirmed")
        state.agent_errors["verification_cleanup"] = reason
        context = getattr(self.patcher, "tool_context", None)
        if context is not None:
            context.execution_uncertain = True
        binding = getattr(self, "_plan_binding", None)
        coordinator = getattr(binding, "coordinator", None) or getattr(
            self, "_entry_coordinator", None
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
            emitter=self._progress_emitter(),
        )

    def _verification_service(self):
        if getattr(self, "_verify_service", None) is None:
            self._verify_service = VerificationService(
                repo_root=self._repo_root,
                policy=self.execution_policy,
                sandbox_context=self._sandbox_context,
                test_target=self._pick_test_path,
                cancel_token=lambda: self._repair_ctx.cancel_token if self._repair_ctx else None,
            )
        return self._verify_service

    def _run_verifier(self, state):
        if self._plan_binding is not None:
            return self._plan_binding.run_verifier(lambda: self._run_verifier_impl(state))
        return self._run_verifier_impl(state)

    def _run_verifier_impl(self, state):
        from src.collaboration.isolation import role_projection

        state.node_timings["verifier_input_projection"] = role_projection(state, "verifier")
        run = self._verification_service().run(state)
        if run.result.all_passed and run.internal.get("completed") is not True:
            run.result.all_passed = False
            run.result.failure_logs.append("verification_execution_unconfirmed")
        if (
            run.result.all_passed
            and run.result.verification_mode == "tests"
            and run.result.total_tests <= 0
        ):
            run.result.all_passed = False
            run.result.failure_logs.append("verification_empty_collection")
        record_verify_timings(state, run)
        return run.result

    def _record_validation_exit(self, state, key):
        run = self._verification_service().run(state)
        code = run.internal.get("exit_code", run.internal.get("pytest_exit_code"))
        if run.internal.get("actual_tier") == "static":
            code = None  # Static success is not a green test baseline.
        setattr(state.control, key, code)
        state.node_timings[key + "_category"] = run.internal.get("category", "unknown")
        state.node_timings[key + "_receipt_id"] = run.internal.get("receipt_id", "")
        state.node_timings[key + "_receipt"] = dict(run.internal)
