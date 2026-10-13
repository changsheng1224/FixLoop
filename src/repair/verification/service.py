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
            run = self._run(state)
            category = str(run.internal.get("category", ""))
            if run.error in {"user_cancel", "cancelled"} or category in {
                "cancelled",
                "user_cancel",
                "interrupted",
            }:
                outcome = "cancelled"
            elif "timeout" in category or "timeout" in str(run.error):
                outcome = "timed_out"
            elif run.internal.get("not_started") or category in {
                "verification_environment_failed",
                "sandbox_unavailable",
                "environment",
            }:
                outcome = "environment_unavailable"
            elif category == "no-tests":
                outcome = "unverified"
            elif (
                run.internal.get("actual_tier") == "static"
                or run.internal.get("completed") is not True
            ):
                outcome = "unverified"
            elif run.result.all_passed and run.result.total_tests > 0:
                outcome = "passed"
            else:
                outcome = "failed"
            run.result.execution_status = outcome
            run.result.all_passed = outcome == "passed"
            return run
        except CancelledError:
            run = _verify_cancelled_run(started)
            run.result.execution_status = "cancelled"
            return run
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
                    and (self.requested_tier == "auto")
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
        self.selected_tier = tier
        return run
