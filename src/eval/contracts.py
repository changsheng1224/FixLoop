"""Structured evaluation contracts and failure attribution."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

EVAL_CONTRACT_VERSION = "1.0"


class FailureClass(StrEnum):
    NONE = "none"
    CONTRACT = "contract"
    BASELINE = "baseline"
    TARGET_TEST = "target_test"
    REGRESSION = "regression"
    ENVIRONMENT = "environment"
    AGENT = "agent"
    EVALUATION = "evaluation"


class FailureCode(StrEnum):
    NONE = "none"
    BASELINE_PASSED = "baseline_passed"
    TARGET_TEST_FAILED = "target_test_failed"
    REGRESSION_DETECTED = "regression_detected"
    ENVIRONMENT_FAILED = "environment_failed"
    AGENT_EXCEPTION = "agent_exception"
    PATCH_EMPTY = "patch_empty"
    HARNESS_FAILED = "harness_failed"
    TRACE_INVALID = "trace_invalid"
    CONTRACT_INVALID = "contract_invalid"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class EvaluationContract:
    """Evidence required before a Case can be marked fixed."""

    version: str = EVAL_CONTRACT_VERSION
    baseline_failed: bool = False
    target_passed: bool = False
    regression_passed: bool = False
    environment_ok: bool = True
    patch_present: bool = False

    @property
    def passed(self) -> bool:
        return (
            self.baseline_failed
            and self.target_passed
            and self.regression_passed
            and self.environment_ok
            and self.patch_present
        )

    def validate(self) -> list[str]:
        errors: list[str] = []
        if self.version != EVAL_CONTRACT_VERSION:
            errors.append("unsupported_contract_version")
        if not self.baseline_failed:
            errors.append(FailureCode.BASELINE_PASSED.value)
        if not self.target_passed:
            errors.append(FailureCode.TARGET_TEST_FAILED.value)
        if not self.regression_passed:
            errors.append(FailureCode.REGRESSION_DETECTED.value)
        if not self.environment_ok:
            errors.append(FailureCode.ENVIRONMENT_FAILED.value)
        if not self.patch_present:
            errors.append(FailureCode.PATCH_EMPTY.value)
        return errors


def attribute_failure(
    contract: EvaluationContract,
    *,
    agent_error: bool = False,
    harness_error: bool = False,
) -> tuple[FailureClass, FailureCode]:
    """Return deterministic primary attribution for a failed evaluation."""
    if contract.passed:
        return FailureClass.NONE, FailureCode.NONE
    if not contract.environment_ok:
        return FailureClass.ENVIRONMENT, FailureCode.ENVIRONMENT_FAILED
    if agent_error:
        return FailureClass.AGENT, FailureCode.AGENT_EXCEPTION
    if harness_error:
        return FailureClass.EVALUATION, FailureCode.HARNESS_FAILED
    if not contract.baseline_failed:
        return FailureClass.CONTRACT, FailureCode.BASELINE_PASSED
    if not contract.patch_present:
        return FailureClass.AGENT, FailureCode.PATCH_EMPTY
    if not contract.target_passed:
        return FailureClass.TARGET_TEST, FailureCode.TARGET_TEST_FAILED
    if not contract.regression_passed:
        return FailureClass.REGRESSION, FailureCode.REGRESSION_DETECTED
    return FailureClass.EVALUATION, FailureCode.UNKNOWN


__all__ = [
    "EVAL_CONTRACT_VERSION",
    "EvaluationContract",
    "FailureClass",
    "FailureCode",
    "attribute_failure",
]
