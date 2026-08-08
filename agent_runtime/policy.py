"""Unified runtime policy and budget/deadline configuration contracts."""

from __future__ import annotations

import hashlib
import json
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, PrivateAttr, model_validator


class BudgetPolicy(BaseModel):
    """One namespaced policy for all run-level resource limits."""

    model_config = ConfigDict(extra="forbid", validate_assignment=True)

    prompt_tokens: int = Field(default=100_000, ge=512, le=2_000_000)
    max_turns: int = Field(default=0, ge=0, le=500)
    max_llm_calls: int = Field(default=0, ge=0, le=2_000)
    max_tool_calls: int = Field(default=0, ge=0, le=5_000)
    max_write_calls: int = Field(default=0, ge=0, le=1_000)
    max_verify_calls: int = Field(default=0, ge=0, le=1_000)
    max_recovery_attempts: int = Field(default=0, ge=0, le=500)
    soft_cost_limit_usd: float = Field(default=0.0, ge=0.0)
    hard_cost_limit_usd: float = Field(default=0.0, ge=0.0)


class DeadlinePolicy(BaseModel):
    """Wall-clock limits propagated through every nested operation."""

    model_config = ConfigDict(extra="forbid", validate_assignment=True)

    repair_s: int = Field(default=0, ge=0, le=86_400)
    step_s: int = Field(default=300, ge=0, le=7_200)
    tool_s: int = Field(default=120, ge=0, le=3_600)
    retry_backoff_cap_s: float = Field(default=120.0, ge=0.0, le=3_600.0)


class LatencySLOPolicy(BaseModel):
    """Latency targets used by the adaptive degradation controller."""

    model_config = ConfigDict(extra="forbid", validate_assignment=True)

    ttft_p95_ms: int = Field(default=0, ge=0, le=3_600_000)
    model_p95_ms: int = Field(default=0, ge=0, le=3_600_000)
    repair_p95_ms: int = Field(default=0, ge=0, le=86_400_000)


class DegradationPolicy(BaseModel):
    """Explicit actions allowed when cost/time budgets become tight."""

    model_config = ConfigDict(extra="forbid")

    enabled: bool = True
    reduce_output_tokens: bool = True
    skip_optional_context: bool = True
    low_latency_provider: str = ""
    max_output_floor: int = Field(default=512, ge=1, le=8_192)


class RuntimePolicy(BaseModel):
    """Canonical policy consumed by CLI, AgentLoop, repair and eval runtimes."""

    model_config = ConfigDict(extra="forbid", validate_assignment=True)

    schema_version: str = "1.0"
    profile: Literal["dev", "prod", "ci"] = "prod"
    provider: str = Field(default="deepseek", min_length=1)
    model: str = Field(default="deepseek-v4-pro", min_length=1)
    max_steps: int = Field(default=6, ge=1, le=500)
    max_new_tokens: int = Field(default=2_048, ge=1, le=8_192)
    prompt_budget: int = Field(default=100_000, ge=512, le=2_000_000)
    hard_cap: int = Field(default=8_000, ge=512, le=2_000_000)
    approval: Literal["auto", "ask", "never"] = "ask"
    temperature: float = Field(default=0.2, ge=0.0, le=2.0)
    json_mode: bool = False
    loop_detect_threshold: int = Field(default=3, ge=0, le=100)
    max_json_retries: int = Field(default=2, ge=0, le=20)
    budget: BudgetPolicy = Field(default_factory=BudgetPolicy)
    deadline: DeadlinePolicy = Field(default_factory=DeadlinePolicy)
    slo: LatencySLOPolicy = Field(default_factory=LatencySLOPolicy)
    degradation: DegradationPolicy = Field(default_factory=DegradationPolicy)
    _provenance: dict[str, str] = PrivateAttr(default_factory=dict)

    @model_validator(mode="after")
    def validate_relationships(self) -> RuntimePolicy:
        if self.hard_cap > self.prompt_budget:
            default_hard_cap = RuntimePolicy.model_fields["hard_cap"].default
            if self.hard_cap == default_hard_cap:
                self.hard_cap = self.prompt_budget
            else:
                raise ValueError("hard_cap must be <= prompt_budget")
        if self.budget.hard_cost_limit_usd and self.budget.soft_cost_limit_usd:
            if self.budget.soft_cost_limit_usd > self.budget.hard_cost_limit_usd:
                raise ValueError("soft_cost_limit_usd must be <= hard_cost_limit_usd")
        if self.degradation.max_output_floor > self.max_new_tokens:
            default_floor = DegradationPolicy.model_fields["max_output_floor"].default
            if self.degradation.max_output_floor == default_floor:
                # Keep legacy small-output test/dev configs valid while still
                # rejecting an explicitly contradictory policy value.
                self.degradation.max_output_floor = self.max_new_tokens
            else:
                raise ValueError("degradation.max_output_floor must be <= max_new_tokens")
        return self

    def effective_budget(self) -> dict[str, int | float]:
        """Return legacy-compatible limits merged with namespaced policy."""
        max_tool_calls = int(getattr(self, "max_tool_calls", 0) or self.budget.max_tool_calls)
        max_write_calls = int(getattr(self, "max_write_calls", 0) or self.budget.max_write_calls)
        max_verify_calls = int(getattr(self, "max_verify_calls", 0) or self.budget.max_verify_calls)
        max_recovery = int(
            getattr(self, "max_recovery_attempts", 0) or self.budget.max_recovery_attempts
        )
        max_llm_calls = int(
            getattr(self, "max_llm_calls_per_repair", 0) or self.budget.max_llm_calls
        )
        return {
            # Prompt tokens are reserved from the run-level ledger using the
            # context builder's estimate before each model dispatch.
            "prompt_tokens": self.budget.prompt_tokens,
            "turns": self.budget.max_turns,
            "llm_calls": max_llm_calls,
            "tool_calls": max_tool_calls,
            "writes": max_write_calls,
            "verifies": max_verify_calls,
            "recoveries": max_recovery,
            "soft_cost_limit_usd": self.budget.soft_cost_limit_usd,
            "hard_cost_limit_usd": self.budget.hard_cost_limit_usd,
        }

    def set_provenance(self, provenance: dict[str, str]) -> RuntimePolicy:
        self._provenance = {str(k): str(v) for k, v in provenance.items()}
        return self

    def snapshot(self) -> dict[str, Any]:
        """Return reproducible config values, source provenance and hash."""
        values = self.model_dump(mode="json")
        payload = {"values": values, "provenance": dict(self._provenance)}
        raw = json.dumps(payload, sort_keys=True, ensure_ascii=True, default=str)
        return {
            "schema_version": self.schema_version,
            "values": values,
            "provenance": dict(self._provenance),
            "config_hash": hashlib.sha256(raw.encode()).hexdigest()[:16],
        }


__all__ = [
    "BudgetPolicy",
    "DeadlinePolicy",
    "DegradationPolicy",
    "LatencySLOPolicy",
    "RuntimePolicy",
]
