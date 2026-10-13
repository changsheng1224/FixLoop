"""Regression coverage for execution policy and host patch transactions."""

import os
import sys
import threading
import time
from pathlib import Path

import pytest

from src.orchestrator import Orchestrator
from src.repair.repo_snapshot import restore_repo_snapshot, snapshot_repo
from src.repair.verification.verify import StaticVerifyStrategy, record_verify_timings
from src.state import RepairPlan, RepairState


def test_snapshot_restores_bytes_presence_and_mode(tmp_path):
    source = tmp_path / "source.py"
    source.write_bytes(b"x = 1\r\nx = 2\n")
    binary = tmp_path / "data.bin"
    binary.write_bytes(b"\x00\xff\x80")
    original_mode = source.stat().st_mode
    before = snapshot_repo(tmp_path)
    source.write_bytes(b"changed\n")
    binary.write_bytes(b"changed binary\x00")
    source.chmod(0o444)
    (tmp_path / "new.txt").write_text("new")
    restore_repo_snapshot(tmp_path, before)
    assert source.read_bytes() == b"x = 1\r\nx = 2\n"
    assert binary.read_bytes() == b"\x00\xff\x80"
    assert source.stat().st_mode == original_mode
    assert not (tmp_path / "new.txt").exists()


def test_snapshot_restores_deleted_files_and_mtime(tmp_path):
    path = tmp_path / "deleted.bin"
    path.write_bytes(b"\x00\xff")
    before = snapshot_repo(tmp_path)
    path.unlink()
    restore_repo_snapshot(tmp_path, before)
    assert path.read_bytes() == b"\x00\xff"
    assert path.stat().st_mtime_ns == before.images["deleted.bin"].mtime_ns


def test_partial_snapshot_preserves_unmanaged_files(tmp_path):
    (tmp_path / "managed.py").write_bytes(b"x=1\n")
    before = snapshot_repo(tmp_path, include=lambda p: p.endswith(".py"))
    (tmp_path / "new.py").write_text("x=2")
    (tmp_path / "notes.txt").write_text("keep")
    restore_repo_snapshot(tmp_path, before)
    assert not (tmp_path / "new.py").exists()
    assert (tmp_path / "notes.txt").read_text() == "keep"


@pytest.mark.parametrize("language", ["python", "javascript", "java"])
def test_static_policy_applies_to_all_validation_phases(tmp_path, monkeypatch, language):
    from src.repair.verification import verify
    from src.repair.verification.verify import VerifyRun
    from src.state import VerificationResult

    seen = []

    def static(self, root, test_path="", cancel_token=None, *, language="python"):
        seen.append(language)
        return VerifyRun(
            VerificationResult(all_passed=True),
            0,
            {
                "completed": True,
                "category": "static_passed",
                "actual_tier": "static",
            },
        )

    monkeypatch.setattr(verify.StaticVerifyStrategy, "run", static)
    monkeypatch.setattr(verify.ProfileVerifyStrategy, "run", lambda *a, **k: pytest.fail("host"))
    monkeypatch.setattr(verify.PytestVerifyStrategy, "run", lambda *a, **k: pytest.fail("pytest"))
    orch = Orchestrator(None, allow_static_verify_fallback=True)
    orch._repo_root = str(tmp_path)
    state = RepairState(issue_input="x", repair_plan=RepairPlan(language=language))
    orch._record_validation_exit(state, "baseline_pytest_code")
    assert orch._run_verifier(state).all_passed
    orch._record_validation_exit(state, "post_patch_pytest_code")
    assert seen == [language] * 3
    assert state.control.baseline_pytest_code is None


def test_container_non_python_never_dispatches_host(tmp_path, monkeypatch):
    from src.repair.verification.verify import ProfileVerifyStrategy

    monkeypatch.setattr(ProfileVerifyStrategy, "run", lambda *a, **k: pytest.fail("host"))
    orch = Orchestrator(None, require_sandbox=True)
    orch._repo_root = str(tmp_path)
    state = RepairState(issue_input="x", repair_plan=RepairPlan(language="java"))
    assert not orch._run_verifier(state).all_passed
    assert state.node_timings["plan_verification_receipt"]["completed"] is False


def test_static_receipt_is_complete_without_test_count(tmp_path):
    (tmp_path / "app.py").write_text("x=1\n")
    run = StaticVerifyStrategy().run(str(tmp_path))
    state = RepairState(issue_input="x")
    record_verify_timings(state, run)
    receipt = state.node_timings["plan_verification_receipt"]
    assert receipt["completed"] is True
    assert receipt["mode"] == "static"
    assert receipt["category"] == "static_passed"
    assert run.result.total_tests == 0


def test_cancel_stops_descendant_before_return(tmp_path):
    from agent_runtime.cancellation import CancellationToken, CancelledError
    from agent_runtime.test_execution import ProcessCleanupUnconfirmedError, run_command

    ready = tmp_path / "ready"
    late = tmp_path / "late"
    child = "import time,pathlib; time.sleep(1.5); pathlib.Path('late').write_text('bad')"
    script = (
        f"import subprocess,sys,time,pathlib; subprocess.Popen([sys.executable,'-c',{child!r}]);"
        "pathlib.Path('ready').write_text('ok'); time.sleep(30)"
    )
    token = CancellationToken()
    errors = []

    def execute():
        try:
            run_command([sys.executable, "-c", script], tmp_path, cancel_token=token)
        except BaseException as exc:
            errors.append(exc)

    worker = threading.Thread(target=execute)
    worker.start()
    deadline = time.monotonic() + 5
    while not ready.exists() and worker.is_alive() and time.monotonic() < deadline:
        time.sleep(0.02)
    assert ready.exists(), errors
    token.cancel()
    worker.join(5)
    assert not worker.is_alive()
    expected = (
        CancelledError
        if os.name == "nt" or sys.platform == "linux"
        else ProcessCleanupUnconfirmedError
    )
    assert len(errors) == 1 and isinstance(errors[0], expected)
    time.sleep(1.6)
    assert not late.exists()


def test_static_plan_completion_is_distinct_from_tests(tmp_path):
    from dataclasses import replace

    from agent_runtime.plan_runtime import Completion
    from tests.plan_support import session_for, simple_plan, through_analysis, write

    (tmp_path / "value.py").write_text("value=1\n")
    session = session_for(tmp_path)
    try:
        plan = simple_plan(session)
        nodes = tuple(
            replace(n, completion=(Completion("static_checked"),)) if n.kind == "verify" else n
            for n in plan.nodes
        )
        session.create(replace(plan, nodes=nodes).seal())
        through_analysis(session)
        session.run_node("edit", lambda a: write(session, a))
        run = StaticVerifyStrategy().run(str(tmp_path))
        state = RepairState(issue_input="x")
        record_verify_timings(state, run)
        session.run_node(
            "verify",
            lambda a: session.verify_result(
                a, run.result.to_dict(), state.node_timings["plan_verification_receipt"]
            ),
        )
        node = session.plan.node("verify")
        assert node.status == "succeeded"
        evidence = session.evidence.get(node.output_evidence_refs[0])
        assert evidence["kind"] == "static_checked"
        assert evidence["receipt"]["total_tests"] == 0
    finally:
        session.close()


@pytest.mark.parametrize(
    ("language", "output", "total", "passed"),
    [
        ("javascript", "Tests: 2 passed, 2 total", 2, 2),
        ("javascript", "3 passing (4ms)\n1 failing", 4, 3),
        ("javascript", "# tests 2\n# pass 2", 2, 2),
        ("rust", "test result: ok. 3 passed; 0 failed; 1 skipped", 4, 3),
        ("go", '{"Test":"TestX","Action":"pass"}\n{"Action":"pass"}', 1, 1),
    ],
)
def test_multilanguage_counts(language, output, total, passed):
    from src.repair.verification.verification_runner import test_counts as counts

    assert counts(output, language) == (total, passed)


def test_profile_receipt_has_real_counts(tmp_path, monkeypatch):
    from src.repair.verification.verify import ProfileVerifyStrategy

    (tmp_path / "package.json").write_text('{"scripts":{"test":"jest"}}')
    monkeypatch.setattr("shutil.which", lambda name: name)
    monkeypatch.setattr(
        "src.repair.verification.verification_runner.run_command",
        lambda *a, **kw: (0, "Tests: 2 passed, 2 total"),
    )
    run = ProfileVerifyStrategy().run(str(tmp_path), language="javascript")
    assert run.result.all_passed
    assert run.result.total_tests == run.result.passed == 2
    assert run.internal["completed"] is True
    assert run.internal["command"]


def test_unconfirmed_cleanup_blocks_rollback_and_cancel(tmp_path, monkeypatch):
    from agent_runtime.test_execution import ProcessCleanupUnconfirmedError

    (tmp_path / "value.py").write_text("x=1\n")
    orch = Orchestrator(None, use_pytest_verify=True)
    orch._repo_root = str(tmp_path)
    before = orch._snapshot_repo()
    (tmp_path / "value.py").write_text("x=2\n")
    service = orch._verification_service()
    monkeypatch.setattr(
        "src.repair.verification.service.PytestVerifyStrategy.run",
        lambda *a, **kw: (_ for _ in ()).throw(ProcessCleanupUnconfirmedError("tree still active")),
    )
    state = RepairState(issue_input="x")
    with pytest.raises(ProcessCleanupUnconfirmedError):
        service.run(state)
    with pytest.raises(ProcessCleanupUnconfirmedError):
        orch._restore_repo_snapshot(before)
    assert not orch._cancel_run_resources(state)
    assert state.status == "recovery_required"
    assert state.recovery_outcome["cleanup_confirmed"] is False
    assert (tmp_path / "value.py").read_text() == "x=2\n"
    retry = orch.repair("retry", repair_timeout_s=0)
    assert retry.status == "recovery_required"


def test_structure_changes_allowed_and_explicit_apis_protected():
    from src.repair.execution.interface_policy import protected_interface_changes

    original = "class Service:\n    async def run(self, *, flag=True): pass\n"
    helper = original + "def helper(): pass\n"
    assert not protected_interface_changes(original, helper, ["Service.run"])
    assert not protected_interface_changes(original, "def replacement(): pass", [])
    changed = original.replace("flag=True", "flag=False")
    assert protected_interface_changes(original, changed, ["Service.run"]) == ["Service.run"]


def test_runtime_routing_has_no_layer_two_imports():
    import ast

    module = Path(__file__).parents[1] / "agent_runtime/linux_sandbox/workspace_routing.py"
    imports = [
        n.module
        for n in ast.walk(ast.parse(module.read_text(encoding="utf-8")))
        if isinstance(n, ast.ImportFrom)
    ]
    assert not any(name and name.startswith("src") for name in imports)


def test_static_l2_plan_and_report_agree(tmp_path, capsys):
    from src.cli import _print_repair_result
    from src.repair_report import RepairReport
    from tests.plan_l2_support import repair_fixture

    repo = tmp_path / "repo"
    orch, state, _ = repair_fixture(repo, full=True)
    orch.use_pytest_verify = False
    orch.allow_static_verify_fallback = True
    from src.repair.verification.execution_policy import resolve_execution_policy

    orch.execution_policy = resolve_execution_policy(
        sandbox_policy="preferred", execution_tier="static", static=True
    )
    result = orch.repair(state.issue_input, repair_timeout_s=0, max_retries=1)
    assert result.status == "pending_verify", result.agent_errors
    assert result.verification_result.verification_mode == "static"
    assert result.verification_result.total_tests == 0
    assert result.node_timings["plan_verification_receipt"]["completed"]
    output = tmp_path / "report"
    output.mkdir()
    report = RepairReport(output, str(repo))
    report.record_state(result, "static")
    assert report.data["status"] == report.data["runtime_status"] == "pending_verify"
    assert report.data["verification_scope"] == "static_checks"
    _print_repair_result(result, False)
    assert "静态检查通过" in capsys.readouterr().out


@pytest.mark.parametrize("confirmed", [True, False])
def test_plan_cancel_requires_cleanup_confirmation(tmp_path, confirmed):
    from tests.plan_support import session_for, simple_plan, through_analysis, write

    (tmp_path / "value.py").write_text("value=1\n")
    session = session_for(tmp_path)
    try:
        session.create(simple_plan(session))
        through_analysis(session)
        session.run_node("edit", lambda a: write(session, a))
        session.run_node(
            "verify",
            lambda a: session.verify_result(
                a,
                {"all_passed": False, "total_tests": 0},
                {
                    "completed": False,
                    "category": "interrupted",
                    "cleanup": "confirmed" if confirmed else "unknown",
                },
            ),
        )
        assert session.plan.node("verify").status == ("cancelled" if confirmed else "uncertain")
    finally:
        session.close()


def test_auto_pins_baseline_fallback_layer(tmp_path, monkeypatch):
    from src.repair.verification.verify import VerifyRun
    from src.state import VerificationResult

    calls = []

    def docker(*a, **kw):
        calls.append("container")
        return VerifyRun(VerificationResult(), 0, {}, "sandbox_unavailable")

    def host(*a, **kw):
        calls.append("host")
        return VerifyRun(
            VerificationResult(all_passed=True, total_tests=1, passed=1),
            0,
            {"actual_tier": "host", "completed": True, "pytest_exit_code": 0},
        )

    monkeypatch.setattr("src.repair.verification.service.DockerVerifyStrategy.run", docker)
    monkeypatch.setattr("src.repair.verification.service.PytestVerifyStrategy.run", host)
    orch = Orchestrator(None, verifier=object(), use_pytest_verify=True)
    orch._repo_root = str(tmp_path)
    state = RepairState(issue_input="x")
    orch._record_validation_exit(state, "baseline_pytest_code")
    orch._run_verifier(state)
    orch._record_validation_exit(state, "post_patch_pytest_code")
    assert calls == ["container", "host", "host", "host"]


def test_cancel_during_host_baseline_restores_after_tree_stops(tmp_path, monkeypatch):
    from agent_runtime.cancellation import CancellationToken

    ready = tmp_path / "ready"
    child = "import time,pathlib;time.sleep(1);pathlib.Path('late').write_text('bad')"
    (tmp_path / "test_slow.py").write_text(
        "import subprocess,sys,pathlib,time\n"
        "def test_wait():\n"
        f"    subprocess.Popen([sys.executable, '-c', {child!r}])\n"
        "    pathlib.Path('ready').write_text('ready')\n"
        "    time.sleep(30)\n"
    )
    orch = Orchestrator(None, use_pytest_verify=True)
    orch._repo_root = str(tmp_path)
    monkeypatch.setattr(orch, "_begin_repair_trace", lambda s: None)
    monkeypatch.setattr(orch, "_parse_issue", lambda _: RepairPlan())
    monkeypatch.setattr(orch, "_run_patcher", lambda _: pytest.fail("patch after cancel"))
    token = CancellationToken()

    def cancel_when_ready():
        deadline = time.monotonic() + 10
        while not ready.exists() and time.monotonic() < deadline:
            time.sleep(0.01)
        token.cancel()

    canceller = threading.Thread(target=cancel_when_ready)
    canceller.start()
    result = orch.repair("fix test_slow.py", cancel_token=token, repair_timeout_s=0)
    canceller.join(2)
    if os.name == "nt":
        assert result.status == "user_cancel", result.agent_errors
        assert not ready.exists()
    else:
        assert result.status == "recovery_required"
    time.sleep(1.1)
    assert not (tmp_path / "late").exists()


def test_missing_checker_is_failed_not_uncertain(tmp_path):
    from tests.plan_support import session_for, simple_plan, through_analysis, write

    (tmp_path / "value.py").write_text("value=1\n")
    session = session_for(tmp_path)
    try:
        session.create(simple_plan(session))
        through_analysis(session)
        session.run_node("edit", lambda a: write(session, a))
        session.run_node(
            "verify",
            lambda a: session.verify_result(
                a,
                {"all_passed": False, "total_tests": 0},
                {
                    "completed": False,
                    "cleanup": "confirmed",
                    "not_started": True,
                    "category": "verification_environment_failed",
                },
            ),
        )
        assert session.plan.node("verify").status == "failed"
    finally:
        session.close()


@pytest.mark.parametrize("fresh", [True, False])
def test_java_counts_require_current_report(tmp_path, monkeypatch, fresh):
    from src.repair.verification.verify import ProfileVerifyStrategy

    (tmp_path / "pom.xml").write_text("<project/>")
    reports = tmp_path / "target/surefire-reports"
    reports.mkdir(parents=True)
    report = reports / "TEST-example.xml"
    report.write_text('<testsuite tests="99" failures="0" errors="0" skipped="0"/>')
    monkeypatch.setattr("shutil.which", lambda name: name)

    def run(*a, **kw):
        if fresh:
            report.write_text('<testsuite tests="2" failures="0" errors="0" skipped="0"/>')
        return 0, ""

    monkeypatch.setattr("src.repair.verification.verification_runner.run_command", run)
    result = ProfileVerifyStrategy().run(str(tmp_path), language="java").result
    assert result.all_passed is fresh
    assert result.total_tests == (2 if fresh else 0)


def test_late_worker_cannot_clear_recovery_fence(tmp_path):
    orch = Orchestrator(None, use_pytest_verify=True)
    orch._repo_root = str(tmp_path)
    service = orch._verification_service()
    service.active.set()
    state = RepairState(issue_input="x")
    assert not orch._cancel_run_resources(state, phase_returned=False)
    service.active.clear()
    assert not service.cleanup_confirmed
    assert orch._abort_repair_if_cancelled(state)
    assert orch.repair("retry", repair_timeout_s=0).status == "recovery_required"


def test_failed_restore_remains_recovery_required(tmp_path, monkeypatch):
    (tmp_path / "value.py").write_text("x=1\n")
    orch = Orchestrator(None)
    orch._repo_root = str(tmp_path)
    before = orch._snapshot_repo()
    monkeypatch.setattr(
        "src.repair.execution.transaction.restore_repo_snapshot",
        lambda *a, **kw: (_ for _ in ()).throw(OSError("cannot restore")),
    )
    monkeypatch.setattr(
        orch, "_repair_impl_with_plan", lambda *a: orch._restore_repo_snapshot(before)
    )
    state = orch._repair_impl(RepairState(issue_input="x"), before)
    assert state.status == "recovery_required"
    assert state.recovery_outcome["reason_code"] == "rollback_outcome_unconfirmed"
    assert not state.recovery_outcome["cleanup_confirmed"]


def test_unknown_execution_is_not_reported_as_test_success(tmp_path, capsys):
    from src.cli import _print_repair_result
    from src.repair_report import RepairReport
    from src.state import CandidatePatch, VerificationResult

    state = RepairState(
        issue_input="x",
        status="fixed",
        candidate_patches=[CandidatePatch(file_path="value.py")],
        verification_result=VerificationResult(all_passed=True, total_tests=2, passed=2),
    )
    report = RepairReport(tmp_path, "test")
    report.record_state(state, "host")
    assert report.data["status"] == "pending_verify"
    assert report.data["verification_scope"] == "unverified"
    _print_repair_result(state, False)
    assert "等待验证" in capsys.readouterr().out
