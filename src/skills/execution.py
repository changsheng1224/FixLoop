"""Runtime-enforced execution gateway for Skills."""

from __future__ import annotations

import hashlib
import inspect
import json
from collections.abc import Callable
from typing import Any

from agent_runtime.cancellation import (
    BlockingDeadlineError,
    CancellationToken,
    CancelledError,
    run_blocking,
)
from agent_runtime.context_runtime import ObservationStore
from src.skills.contract import (
    SideEffectLevel,
    canonical_from_executable,
    resolve_evidence,
    validate_json_contract,
)
from src.skills.executable import RUNNERS
from src.skills.feedback import SkillFeedbackLedger, SkillUsageStage
from src.skills.invocation import (
    SkillErrorCode,
    SkillExecutionResult,
    SkillInvocation,
    SkillInvocationStatus,
)
from src.skills.registry import SkillRegistry, get_default_executable_registry

TraceFn = Callable[[str, dict[str, Any], str | None], None]
AuthorizeFn = Callable[[str], bool]


class SkillExecutionGateway:
    """Validate, admit, execute and observe one Skill invocation.

    Guidance can fail soft before this boundary. Executable capabilities fail
    closed when identity, schema, permission or side-effect state is unclear.
    """

    def __init__(
        self,
        registry: SkillRegistry | None = None,
        *,
        state: dict[str, Any] | None = None,
        workspace_root: str = "",
        authorize_tool: AuthorizeFn | None = None,
        trace: TraceFn | None = None,
    ) -> None:
        self.registry = registry or get_default_executable_registry()
        self.state = state if state is not None else {}
        self.observations = ObservationStore(self.state, workspace_root)
        self.authorize_tool = authorize_tool
        self.trace = trace
        self.feedback = SkillFeedbackLedger(self.state, trace=trace)

    def _emit(self, event: str, invocation: SkillInvocation, status: str | None = None) -> None:
        if self.trace is not None:
            self.trace(event, invocation.to_dict(), status)

    def _finish(self, result: SkillExecutionResult) -> SkillExecutionResult:
        invocation = result.invocation
        ledger = self.state.setdefault("action_ledger", [])
        action = next(
            (
                item
                for item in reversed(ledger)
                if item.get("action_id") == invocation.invocation_id
            ),
            None,
        )
        if action is None:
            action = {
                "action_id": invocation.invocation_id,
                "action_type": "skill",
                "tool": f"skill:{invocation.skill_name}",
                "args_hash": invocation.input_hash,
                "side_effect": invocation.side_effect_level,
                "replay_policy": (
                    "retry_idempotent"
                    if invocation.idempotency_key
                    else "never_replay"
                    if invocation.side_effect_level not in ("", SideEffectLevel.NONE.value)
                    else "revalidate"
                ),
            }
            ledger.append(action)
        status_map = {
            SkillInvocationStatus.SUCCEEDED.value: "acknowledged",
            SkillInvocationStatus.SIDE_EFFECT_UNCERTAIN.value: "uncertain",
            SkillInvocationStatus.FAILED.value: "failed",
            SkillInvocationStatus.INCOMPLETE.value: "failed",
            SkillInvocationStatus.TIMED_OUT.value: "uncertain",
            SkillInvocationStatus.CANCELLED.value: "uncertain",
        }
        action.update(
            {
                "status": status_map.get(invocation.status, invocation.status),
                "idempotency_key": invocation.idempotency_key,
                "receipt": invocation.side_effect_receipt,
                "observation_id": invocation.observation_id,
                "error": invocation.error_message,
            }
        )
        self.state["action_ledger"] = ledger[-100:]
        if (
            invocation.status
            in {
                SkillInvocationStatus.FAILED.value,
                SkillInvocationStatus.INCOMPLETE.value,
                SkillInvocationStatus.TIMED_OUT.value,
            }
            and not result.invocation.fail_closed
        ):
            self._emit("skill_fallback", result.invocation, "ok")
        self.state.setdefault("skill_invocations", []).append(invocation.to_dict())
        self.state["skill_invocations"] = self.state["skill_invocations"][-100:]
        return result

    def execute(
        self,
        name: str,
        args: dict[str, Any] | None = None,
        *,
        pinned_version: str = "",
        runner: Callable[..., dict[str, Any]] | None = None,
        tool_bindings: dict[str, Callable[..., Any]] | None = None,
        runtime_allowed_tools: set[str] | None = None,
        cancel_token: CancellationToken | None = None,
        timeout_s: float | None = None,
        idempotency_key: str = "",
        dry_run: bool = False,
        read_before_write: bool = False,
    ) -> SkillExecutionResult:
        args = dict(args or {})
        invocation = SkillInvocation(skill_name=name, input_summary=str(args)[:500])
        input_raw = json.dumps(args, sort_keys=True, ensure_ascii=True, default=str)
        invocation.input_hash = hashlib.sha256(input_raw.encode()).hexdigest()[:16]
        self._emit("skill_discovered", invocation)
        legacy = self.registry.get(name, pinned_version or None)
        if legacy is None:
            invocation.fail(
                SkillInvocationStatus.FAILED,
                SkillErrorCode.SKILL_NOT_FOUND,
                f"unknown skill: {name}",
            )
            self._emit("skill_failed", invocation, "error")
            return self._finish(SkillExecutionResult(invocation))
        spec = canonical_from_executable(legacy)
        invocation.skill_version = spec.version
        invocation.content_hash = spec.content_hash
        invocation.side_effect_level = spec.side_effect_level.value
        invocation.fail_closed = spec.fail_closed()
        invocation.idempotency_key = idempotency_key
        if pinned_version and pinned_version != spec.version:
            invocation.fail(
                SkillInvocationStatus.FAILED,
                SkillErrorCode.VERSION_UNAVAILABLE,
                f"pinned={pinned_version}, resolved={spec.version}",
            )
            self._emit("skill_failed", invocation, "error")
            return self._finish(SkillExecutionResult(invocation))
        if not spec.permits_new_invocation():
            invocation.fail(
                SkillInvocationStatus.FAILED,
                SkillErrorCode.VERSION_UNAVAILABLE,
                f"lifecycle={spec.lifecycle.value}",
            )
            self._emit("skill_failed", invocation, "error")
            return self._finish(SkillExecutionResult(invocation))
        if spec.trust_level.value == "untrusted":
            invocation.fail(
                SkillInvocationStatus.FAILED,
                SkillErrorCode.PERMISSION_DENIED,
                "untrusted executable skills are guidance-only",
            )
            self._emit("skill_failed", invocation, "error")
            return self._finish(SkillExecutionResult(invocation))
        issues = validate_json_contract(args, spec.input_schema)
        missing_preconditions = [
            path for path in spec.preconditions if not resolve_evidence(args, path)[0]
        ]
        if missing_preconditions:
            issues.append(f"missing preconditions: {missing_preconditions}")
        if issues:
            invocation.fail(
                SkillInvocationStatus.FAILED,
                SkillErrorCode.INPUT_INVALID,
                "; ".join(issues),
            )
            self._emit("skill_failed", invocation, "error")
            return self._finish(SkillExecutionResult(invocation))

        bindings = dict(tool_bindings or {})
        declared = set(spec.allowed_tools)
        runtime_allowed = set(runtime_allowed_tools or set())
        unauthorized = [
            tool
            for tool in bindings
            if tool not in declared
            or tool not in runtime_allowed
            or (self.authorize_tool is not None and not self.authorize_tool(tool))
        ]
        if unauthorized:
            invocation.fail(
                SkillInvocationStatus.FAILED,
                SkillErrorCode.PERMISSION_DENIED,
                f"tools not admitted: {sorted(unauthorized)}",
            )
            self._emit("skill_failed", invocation, "error")
            return self._finish(SkillExecutionResult(invocation))
        invocation.admitted_tools = sorted(bindings)
        if spec.requires_read_before_write and not dry_run and not read_before_write:
            invocation.fail(
                SkillInvocationStatus.SIDE_EFFECT_UNCERTAIN,
                SkillErrorCode.SIDE_EFFECT_UNCERTAIN,
                "read-before-write evidence is required",
            )
            self._emit("skill_failed", invocation, "error")
            return self._finish(SkillExecutionResult(invocation))
        if (
            spec.side_effect_level is not SideEffectLevel.NONE
            and not idempotency_key
            and not dry_run
        ):
            invocation.fail(
                SkillInvocationStatus.SIDE_EFFECT_UNCERTAIN,
                SkillErrorCode.SIDE_EFFECT_UNCERTAIN,
                "side-effecting skill requires idempotency_key or dry_run",
            )
            self._emit("skill_failed", invocation, "error")
            return self._finish(SkillExecutionResult(invocation))
        prior = self.state.setdefault("skill_idempotency", {}).get(idempotency_key)
        if idempotency_key and prior and prior.get("status") == "succeeded":
            if prior.get("input_hash") != invocation.input_hash:
                invocation.fail(
                    SkillInvocationStatus.SIDE_EFFECT_UNCERTAIN,
                    SkillErrorCode.SIDE_EFFECT_UNCERTAIN,
                    "idempotency key was previously used with different input",
                )
                self._emit("skill_failed", invocation, "error")
                return self._finish(SkillExecutionResult(invocation))
            restored = SkillInvocation(**prior)
            return self._finish(SkillExecutionResult(restored, reused=True))

        invocation.transition(SkillInvocationStatus.ADMITTED)
        self._emit("skill_admitted", invocation, "ok")
        tool_limit = spec.budget.max_tool_calls
        call_count = 0

        def bind(tool_name: str, fn: Callable[..., Any]) -> Callable[..., Any]:
            def governed(payload: dict[str, Any]) -> Any:
                nonlocal call_count
                if cancel_token is not None:
                    cancel_token.check()
                if call_count >= tool_limit:
                    raise RuntimeError(SkillErrorCode.TOOL_BUDGET_EXHAUSTED.value)
                call_count += 1
                call = {"tool": tool_name, "index": call_count}
                invocation.tool_calls.append(call)
                self._emit("skill_tool_called", invocation, "ok")
                return fn(payload)

            return governed

        governed_bindings = (
            {}
            if dry_run and spec.side_effect_level is not SideEffectLevel.NONE
            else {name: bind(name, fn) for name, fn in bindings.items()}
        )
        selected_runner = runner or RUNNERS.get(name)
        if selected_runner is None:
            invocation.fail(
                SkillInvocationStatus.FAILED,
                SkillErrorCode.SKILL_NOT_FOUND,
                f"no runner for skill: {name}",
            )
            self._emit("skill_failed", invocation, "error")
            return self._finish(SkillExecutionResult(invocation))
        parameters = inspect.signature(selected_runner).parameters
        runner_kwargs = {
            key: value for key, value in governed_bindings.items() if key in parameters
        }

        invocation.transition(SkillInvocationStatus.RUNNING)
        self._emit("skill_started", invocation, "ok")
        self.feedback.record_stage(
            skill_name=name,
            skill_version=spec.version,
            stage=SkillUsageStage.INVOKED,
            invocation_id=invocation.invocation_id,
        )
        try:
            effective_timeout = timeout_s if timeout_s is not None else spec.budget.timeout_s
            attempts = 0
            while True:
                try:
                    output = run_blocking(
                        lambda: selected_runner(args, **runner_kwargs),
                        cancel_token=cancel_token,
                        timeout_s=effective_timeout,
                    )
                    break
                except (ConnectionError, TimeoutError, OSError):
                    if attempts >= spec.budget.max_retries:
                        raise
                    attempts += 1
                    invocation.retry_count = attempts
        except CancelledError as exc:
            invocation.fail(SkillInvocationStatus.CANCELLED, SkillErrorCode.CANCELLED, str(exc))
            self._emit("skill_failed", invocation, "cancelled")
            return self._finish(SkillExecutionResult(invocation))
        except BlockingDeadlineError as exc:
            invocation.fail(SkillInvocationStatus.TIMED_OUT, SkillErrorCode.TIMEOUT, str(exc))
            self._emit("skill_failed", invocation, "error")
            return self._finish(SkillExecutionResult(invocation))
        except Exception as exc:
            code = (
                SkillErrorCode.TOOL_BUDGET_EXHAUSTED
                if SkillErrorCode.TOOL_BUDGET_EXHAUSTED.value in str(exc)
                else SkillErrorCode.RUNNER_FAILED
            )
            invocation.fail(SkillInvocationStatus.FAILED, code, str(exc))
            self._emit("skill_failed", invocation, "error")
            return self._finish(SkillExecutionResult(invocation))

        if not isinstance(output, dict):
            invocation.fail(
                SkillInvocationStatus.FAILED,
                SkillErrorCode.OUTPUT_INVALID,
                "runner output must be an object",
            )
            self._emit("skill_failed", invocation, "error")
            return self._finish(SkillExecutionResult(invocation))
        output_issues = validate_json_contract(output, spec.output_schema)
        encoded = json.dumps(output, ensure_ascii=False, default=str)
        if len(encoded) > spec.budget.max_output_chars:
            output_issues.append("$: output exceeds max_output_chars")
        if output_issues:
            invocation.fail(
                SkillInvocationStatus.FAILED,
                SkillErrorCode.OUTPUT_INVALID,
                "; ".join(output_issues),
            )
            self._emit("skill_failed", invocation, "error")
            return self._finish(SkillExecutionResult(invocation, output=output))
        missing = [
            path
            for path in spec.completion_evidence + spec.postconditions
            if not resolve_evidence(output, path)[0]
        ]
        if missing:
            invocation.fail(
                SkillInvocationStatus.INCOMPLETE,
                SkillErrorCode.EVIDENCE_MISSING,
                f"missing evidence: {missing}",
            )
            self._emit("skill_failed", invocation, "error")
            return self._finish(SkillExecutionResult(invocation, output=output))
        invocation.completion_evidence = list(spec.completion_evidence)
        observation = self.observations.put(
            f"skill:{name}",
            args,
            encoded,
            summary=f"{name}@{spec.version} completed",
            source_version=f"{spec.version}:{spec.content_hash[:12]}",
            structured_facts=[{"kind": "skill_output", "value": output}],
            provenance={
                "kind": "skill",
                "skill_name": name,
                "skill_version": spec.version,
                "content_hash": spec.content_hash,
                "invocation_id": invocation.invocation_id,
            },
            status="succeeded",
            redact=True,
        )
        invocation.observation_id = observation.observation_id
        invocation.output_ref = observation.raw_ref
        if spec.side_effect_level is not SideEffectLevel.NONE:
            invocation.side_effect_receipt = str(
                output.get("receipt") or observation.observation_id
            )
        invocation.transition(SkillInvocationStatus.SUCCEEDED)
        if idempotency_key:
            self.state["skill_idempotency"][idempotency_key] = invocation.to_dict()
        self._emit("skill_completed", invocation, "ok")
        return self._finish(
            SkillExecutionResult(invocation, output=output, observation=observation.__dict__)
        )


def execute_skill(
    name: str,
    args: dict[str, Any] | None = None,
    *,
    gateway: SkillExecutionGateway | None = None,
    **kwargs: Any,
) -> SkillExecutionResult:
    """Public governed entry point; raw Runner dispatch remains internal."""
    return (gateway or SkillExecutionGateway()).execute(name, args, **kwargs)
