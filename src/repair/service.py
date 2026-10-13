"""The sole application entry point for repair execution."""

from __future__ import annotations

from dataclasses import dataclass

from src.repair.phase_clock import PhaseTimeoutConfig
from src.repair.session import ExecutionSession


@dataclass(frozen=True)
class RepairRequest:
    issue: str
    max_retries: int = 3
    timeout_s: int = 180
    phase_timeouts: PhaseTimeoutConfig | None = None
    cancel_token: object = None
    resume_run_id: str = ""
    run_id: str = ""
    workspace_ready: object = None
    verification_target: str = ""
    verification_min_tests: int = 1

    def __post_init__(self):
        if not isinstance(self.issue, str) or not self.issue.strip():
            raise ValueError("repair issue must be nonempty text")
        if self.max_retries < 1 or self.timeout_s < 0:
            raise ValueError("invalid repair limits")
        if self.verification_min_tests < 1:
            raise ValueError("verification_min_tests must be positive")
        if self.run_id and self.resume_run_id:
            raise ValueError("run_id and resume_run_id are mutually exclusive")


class RepairService:
    def __init__(self, session: ExecutionSession):
        self.session = session

    def run(self, request: RepairRequest):
        if not isinstance(request, RepairRequest):
            raise TypeError("RepairService.run requires RepairRequest")
        session = self.session
        if not session.run_lock.acquire(blocking=False):
            raise RuntimeError("repair session already executing")
        try:
            session.current_state = None
            session.request = request
            return session.lifecycle.execute(
                request.issue,
                max_retries=request.max_retries,
                repair_timeout_s=request.timeout_s,
                phase_timeouts=request.phase_timeouts,
                cancel_token=request.cancel_token,
                resume_run_id=request.resume_run_id,
                run_id=request.run_id,
                workspace_ready=request.workspace_ready,
            )
        except Exception as exc:
            from agent_runtime.cancellation import BlockingDeadlineError, CancelledError
            from src.state import RepairState

            state = session.current_state or RepairState(
                issue_input=request.issue,
                max_retries=request.max_retries,
                repair_run_id=request.run_id or request.resume_run_id,
            )
            state.agent_errors["runtime"] = str(exc)
            if session.plan_binding is not None:
                try:
                    session.lifecycle.close_sandbox_backend_before_binding_close(state)
                    session.plan_binding.close()
                except Exception as cleanup_error:
                    state.agent_errors["runtime_cleanup"] = str(cleanup_error)
                    state.set_status("recovery_required", "runtime_cleanup_unconfirmed")
                finally:
                    session.plan_binding = None
                try:
                    session.coordinator.release_repair_resources()
                except Exception as cleanup_error:
                    state.agent_errors["runtime_cleanup"] = str(cleanup_error)
                    state.set_status("recovery_required", "runtime_cleanup_unconfirmed")
            if state.status != "recovery_required":
                if isinstance(exc, CancelledError):
                    state.control.user_cancel = True
                    state.set_status("user_cancel", "execution_cancelled")
                elif isinstance(exc, (TimeoutError, BlockingDeadlineError)):
                    state.set_status("timeout", "execution_deadline_exceeded")
                else:
                    state.set_status("failed", "execution_failed")
            return state
        finally:
            session.request = None
            session.run_lock.release()
