"""P2 verification routing and pre-P3 product gate."""

from types import SimpleNamespace

import pytest

from agent_runtime.linux_sandbox.models import SandboxResult
from agent_runtime.tool_context import ToolContext
from src.repair.pipeline import _record_pytest_exit
from src.repair.verification.verify import BwrapVerifyStrategy
from src.state import RepairState


class Backend:
    def __init__(self, result):
        self.result = result
        self.requests = []

    def execute(self, request):
        self.requests.append(request)
        return self.result


@pytest.mark.parametrize(
    ("code", "category", "passed"),
    [(0, "passed", True), (1, "failed", False), (5, "no-tests", False), (4, "environment", False)],
)
def test_verify_exit_categories(tmp_path, code, category, passed):
    backend = Backend(
        SandboxResult(
            "completed",
            exit_code=code,
            cleanup="confirmed",
            receipt_id="receipt",
            actual_backend="linux_sandbox",
        )
    )
    ctx = ToolContext(root=str(tmp_path), sandbox_backend=backend)
    run = BwrapVerifyStrategy(ctx).run(str(tmp_path))
    assert run.result.all_passed is passed
    assert run.internal["category"] == category
    assert run.internal["receipt_id"] == "receipt"
    assert backend.requests[0].operation == "pytest"
    assert backend.requests[0].argv[:4] == ("/toolchain/bin/python", "-I", "-m", "pytest")


def test_no_host_fallback_and_baseline_receipt(tmp_path, monkeypatch):
    monkeypatch.setattr("src.repair.pipeline.run_pytest", lambda *_: pytest.fail("host bypass"))
    backend = Backend(
        SandboxResult(
            "completed",
            exit_code=1,
            cleanup="confirmed",
            receipt_id="baseline",
            actual_backend="linux_sandbox",
        )
    )
    ctx = ToolContext(root=str(tmp_path), sandbox_backend=backend)
    state = RepairState(issue_input="failure")
    _record_pytest_exit(state, str(tmp_path), "baseline_pytest_code", ctx)
    assert state.control.baseline_pytest_code == 1
    assert state.node_timings["baseline_pytest_code_receipt_id"] == "baseline"
    backend.result = SandboxResult("rejected", error_code="namespace_unavailable")
    with pytest.raises(RuntimeError, match="sandbox pytest unavailable"):
        _record_pytest_exit(state, str(tmp_path), "post_patch_pytest_code", ctx)


def test_non_python_is_not_sent_to_backend(tmp_path):
    backend = Backend(SandboxResult("completed", exit_code=0, cleanup="confirmed"))
    run = BwrapVerifyStrategy(ToolContext(root=str(tmp_path), sandbox_backend=backend)).run(
        str(tmp_path),
        language="java",
    )
    assert run.error == "verification_environment_failed"
    assert backend.requests == []


def test_missing_actual_backend_or_receipt_fails_closed(tmp_path):
    for result in (
        SandboxResult("completed", exit_code=0, cleanup="confirmed", receipt_id="r"),
        SandboxResult(
            "completed", exit_code=0, cleanup="confirmed", actual_backend="linux_sandbox"
        ),
    ):
        backend = Backend(result)
        run = BwrapVerifyStrategy(ToolContext(root=str(tmp_path), sandbox_backend=backend)).run(
            str(tmp_path)
        )
        assert not run.result.all_passed
        assert run.internal["category"] == "environment"


def test_factory_rejects_profile_before_workspace_or_model_setup(tmp_path):
    from src.repair_factory import RequiredVerifierError, wire_orchestrator

    with pytest.raises(RequiredVerifierError, match="P3"):
        wire_orchestrator(None, str(tmp_path), execution_backend="wsl_bwrap")


def test_factory_rejects_conflicting_tier(tmp_path):
    from src.repair_factory import RequiredVerifierError, wire_orchestrator

    with pytest.raises(RequiredVerifierError, match="conflicts"):
        wire_orchestrator(None, str(tmp_path), execution_backend="wsl_bwrap", execution_tier="host")


def test_cli_profile_gate_precedes_model_setup(monkeypatch, capsys):
    import sys

    from src.cli import main
    from src.cli_exit_codes import REPAIR_EXIT_CONFIG

    monkeypatch.setattr("src.cli.load_dotenv", lambda: pytest.fail("model setup reached"))
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "src.cli",
            "repair",
            "--issue",
            "broken",
            "--execution-backend",
            "wsl_bwrap",
        ],
    )
    assert main() == REPAIR_EXIT_CONFIG
    assert "P3 external state_root" in capsys.readouterr().err


def test_orchestrator_final_route_has_no_host_fallback(tmp_path, monkeypatch):
    from src.orchestrator import Orchestrator

    backend = Backend(
        SandboxResult(
            "completed",
            exit_code=0,
            cleanup="confirmed",
            receipt_id="final",
            actual_backend="linux_sandbox",
        )
    )
    ctx = ToolContext(root=str(tmp_path), sandbox_backend=backend)
    orch = Orchestrator(SimpleNamespace(_cwd=str(tmp_path), tool_context=ctx), sandbox_context=ctx)
    monkeypatch.setattr(orch, "_pick_test_path", lambda _: "")
    monkeypatch.setattr(
        "src.repair.verification.verify.run_profile", lambda *_a, **_kw: pytest.fail("host bypass")
    )
    state = RepairState(issue_input="failure")
    assert orch._run_verifier_python(state).all_passed
    assert backend.requests[0].operation == "pytest"
    assert state.node_timings["phases_internal"]["verify"]["receipt_id"] == "final"
    with pytest.raises(ValueError, match="P3"):
        orch.repair("failure")
