"""Public execution outcomes, explicit limits and cancellable server waits."""

import subprocess
from types import SimpleNamespace

import pytest

from agent_runtime.cancellation import CancellationToken, CancelledError
from agent_runtime.providers import retry_policy
from src.repair.config import RepairConfig
from src.repair.verification.service import VerificationService
from src.repair.verification.verify import PytestVerifyStrategy, VerifyRun
from src.state import RepairState, VerificationResult


def test_server_wait_cannot_be_shortened(monkeypatch):
    clock = [0.0]
    monkeypatch.setattr(retry_policy.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(
        retry_policy.time, "sleep", lambda delay: clock.__setitem__(0, clock[0] + delay)
    )
    delay = retry_policy.compute_rate_limit_delay(0, 60)
    retry_policy.wait_retry(delay, cancel_token=CancellationToken())
    assert clock[0] >= 60


def test_server_wait_exceeds_deadline_without_sleep(monkeypatch):
    monkeypatch.setattr(retry_policy.time, "monotonic", lambda: 100)
    monkeypatch.setattr(retry_policy.time, "sleep", lambda _: pytest.fail("should not wait"))
    with pytest.raises(TimeoutError, match="retry_deadline_exhausted"):
        retry_policy.wait_retry(60, deadline=159)


def test_server_wait_observes_cancellation(monkeypatch):
    clock, token = [0.0], CancellationToken()

    def sleep(delay):
        clock[0] += delay
        token.cancel("user")

    monkeypatch.setattr(retry_policy.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(retry_policy.time, "sleep", sleep)
    with pytest.raises(CancelledError):
        retry_policy.wait_retry(60, cancel_token=token)
    assert clock[0] < 1


@pytest.mark.parametrize(
    "outcome,internal,error,passed,total",
    [
        ("passed", {"completed": True}, None, True, 1),
        ("failed", {"completed": True}, None, False, 1),
        ("environment_unavailable", {"not_started": True}, "unavailable", False, 0),
        (
            "environment_unavailable",
            {"completed": True, "category": "environment"},
            "environment",
            False,
            0,
        ),
        ("unverified", {"completed": True, "category": "no-tests"}, "no-tests", False, 0),
        ("timed_out", {"category": "timeout"}, "timeout", False, 0),
        ("cancelled", {"category": "cancelled"}, "cancelled", False, 0),
        ("cancelled", {"category": "interrupted"}, "interrupted", False, 0),
        ("unverified", {"completed": True, "actual_tier": "static"}, None, True, 0),
    ],
)
def test_real_verification_outcomes(monkeypatch, outcome, internal, error, passed, total):
    service = VerificationService(
        repo_root=".",
        policy=SimpleNamespace(requested_tier="host"),
        sandbox_context=None,
        test_target=lambda _: "",
        cancel_token=lambda: None,
    )
    run = VerifyRun(VerificationResult(all_passed=passed, total_tests=total), 1, internal, error)
    monkeypatch.setattr(service, "_run", lambda _: run)
    result = service.run(RepairState(issue_input="fix")).result
    assert result.execution_status == outcome
    assert result.all_passed is (outcome == "passed")
    assert not service.active.is_set()


@pytest.mark.parametrize(
    "error,category",
    [
        (OSError("unavailable"), "verification_environment_failed"),
        (subprocess.TimeoutExpired("pytest", 1), "timeout"),
    ],
)
def test_host_execution_failure_is_classified(monkeypatch, error, category):
    def fail(*args, **kwargs):
        raise error

    monkeypatch.setattr("src.repair.verification.verify.run_pytest", fail)
    run = PytestVerifyStrategy().run(".")
    assert run.internal["category"] == category
    assert not run.result.all_passed


def test_explicit_exploration_limits_and_provenance():
    config = RepairConfig(exploration={"tool_calls": 2, "tokens": 10000})
    assert config.snapshot()["values"]["exploration"]["tool_calls"] == 2
    with pytest.raises(ValueError):
        RepairConfig(exploration={"tool_calls": 5})


@pytest.mark.parametrize("version", [None, "1.0", "2.0"])
def test_old_checkpoint_and_session_versions_are_rejected(tmp_path, version):
    import json

    from agent_runtime.session_contract import CheckpointEnvelope
    from agent_runtime.session_store import SessionStore

    raw = {"id": "old", "schema_version": version}
    with pytest.raises(ValueError, match="unsupported_checkpoint_envelope_version"):
        CheckpointEnvelope.from_dict(raw)
    store = SessionStore(str(tmp_path))
    store.ensure_dir()
    (store.sessions_dir / "old.json").write_text(json.dumps(raw), encoding="utf-8")
    with pytest.raises(ValueError, match="unsupported_session_version"):
        store.load("old")
    with pytest.raises(ValueError, match="unsupported_session_version"):
        store.save(raw)


@pytest.mark.parametrize("status", ["completed", "unknown", "success", ""])
def test_unsupported_verification_status_is_rejected(status):
    with pytest.raises(ValueError, match="unsupported verification execution_status"):
        VerificationResult.from_dict({"schema_version": "2.0", "execution_status": status})


@pytest.mark.parametrize(
    "status", ["failed", "environment_unavailable", "timed_out", "cancelled", "unverified"]
)
def test_unconfirmed_verification_cannot_be_fixed(status):
    from src.repair.verification.termination import resolve_terminal_status

    state = RepairState(issue_input="fix", status="fixed")
    state.verification_result = VerificationResult(
        execution_status=status, all_passed=True, total_tests=1
    )
    assert resolve_terminal_status(state) != "fixed"


def test_request_minimum_test_count_is_enforced():
    from src.repair.service import RepairRequest
    from src.repair.verification.controller import VerificationController

    result = VerificationResult(all_passed=True, total_tests=1)
    service = VerificationService(
        repo_root=".",
        policy=SimpleNamespace(requested_tier="host"),
        sandbox_context=None,
        test_target=lambda _: "",
        cancel_token=lambda: None,
    )
    service._run = lambda _: VerifyRun(result, 1, {"completed": True}, None)
    session = SimpleNamespace(
        request=RepairRequest("fix", verification_min_tests=2),
        verify_service=service,
    )
    controller = VerificationController(session)
    assert not controller.run_verifier_impl(RepairState(issue_input="fix")).all_passed


def test_unlimited_config_does_not_install_hidden_tool_quotas():
    from agent_runtime.config import AgentConfig
    from agent_runtime.tool_executor import QuotaEnforcer

    quota = QuotaEnforcer.from_policy(AgentConfig())
    for _ in range(60):
        assert quota.check("read_file")
        quota.record("read_file")
    assert quota.check("write_file")
    assert quota.quota_summary()["groups"]["write"]["limit"] is None


def test_shell_limit_is_independent_of_grouped_verification_limit():
    from agent_runtime.config import AgentConfig
    from agent_runtime.tool_executor import QuotaEnforcer

    quota = QuotaEnforcer.from_policy(AgentConfig(budget={"max_shell_calls": 1}))
    assert quota.check("run_shell")
    quota.record("run_shell")
    assert not quota.check("run_shell")
    assert quota.check("quick_test")


def test_explicit_context_projection_is_independent_of_role_label():
    from agent_runtime.context_runtime import ContextItem, ContextRequest, ContextViewPolicy

    item = ContextItem("memory", "memory", "private context")
    policy = ContextViewPolicy.for_request(ContextRequest(role="verifier"))
    assert policy.allows(item)
    restricted = ContextViewPolicy.for_request(ContextRequest(allowed_kinds=("source",)))
    assert not restricted.allows(item)


def test_missing_evaluation_resource_has_explicit_error(tmp_path):
    from src.eval.case_io import read_expected_patch

    with pytest.raises(FileNotFoundError, match="required evaluation resource missing"):
        read_expected_patch(tmp_path)
