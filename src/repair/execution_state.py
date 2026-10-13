"""Authoritative execution receipts and recovery inputs, independent of metrics."""

from pydantic import BaseModel, ConfigDict, Field


class VerificationReceipt(BaseModel):
    model_config = ConfigDict(extra="forbid", validate_assignment=True)

    command: list = Field(default_factory=list)
    mode: str = "tests"
    operation: str = ""
    checked_files: int = Field(default=0, ge=0)
    steps: list[dict] = Field(default_factory=list)
    cleanup: str = "unknown"
    completed: bool = False
    not_started: bool = False
    exit_code: int | None = None
    category: str = "unknown"
    receipt_id: str = ""
    verification_binding: dict = Field(default_factory=dict)


class RepairExecutionState(BaseModel):
    model_config = ConfigDict(extra="forbid", validate_assignment=True)

    verification_receipt: VerificationReceipt = Field(default_factory=VerificationReceipt)
    phase_revision: int = Field(default=0, ge=0)
    run_entry: dict = Field(default_factory=dict)
    repo_root: str = ""
    sandbox_task_binding: dict = Field(default_factory=dict)
    model_budget: dict = Field(default_factory=dict)
    model_deadline: dict = Field(default_factory=dict)
    worktree_recovery: dict = Field(default_factory=dict)
    failure_ledger: dict = Field(default_factory=dict)
