"""Durable repair control data, separate from timings and telemetry."""

from pydantic import BaseModel, ConfigDict, Field


class RepairControl(BaseModel):
    model_config = ConfigDict(extra="forbid", validate_assignment=True)

    user_cancel: bool = False
    repair_timeout: float = 0
    phase_timeout: str = ""
    coordination_status: str = ""
    introduced_regression: bool = False
    baseline_pytest_code: int | None = None
    post_patch_pytest_code: int | None = None
    verify_skipped: bool = False
    verify_skipped_reason: str = ""
    recovery_outcome: dict = Field(default_factory=dict)
    repair_failure_decision: dict = Field(default_factory=dict)
    checkpoint_next_action: str = ""
    resume_workspace_stale: bool = False
    plan_blocked: bool = False
    patcher_terminal_status: str = ""
    patcher_parse_failed: bool = False
    patcher_apply_failed: bool = False
    patcher_write_attempted: bool | None = None
    patcher_write_rejected: bool = False
    patcher_export_failed: bool = False
    stop_loss_snapshot: dict = Field(default_factory=dict)
    no_progress_warning: dict = Field(default_factory=dict)
    consecutive_env_fails: int = Field(default=0, ge=0)
    verify_env_early_stop: bool = False
    allowed_edit: list[str] = Field(default_factory=list)
    structured_verify_feedback: dict = Field(default_factory=dict)
    verify_failed_nodeids: list[str] = Field(default_factory=list)
    verify_bucket: str = ""
    verify_target: str = ""
    stop_loss: str = ""
    stop_loss_early: bool = False
    plan_checkpoint: dict = Field(default_factory=dict)
    exploration_checkpoint: dict = Field(default_factory=dict)
    patch_retry_fingerprints: dict[str, int] = Field(default_factory=dict)

    def reset(self, field: str) -> None:
        """Reset per-attempt fields to their declared defaults."""
        if field not in type(self).model_fields:
            raise ValueError(f"unknown repair control field: {field}")
        setattr(self, field, type(self).model_fields[field].get_default(call_default_factory=True))
