"""Run the S7 repair main flow against a retained WSL2/bwrap environment.

The probe uses a local Git fixture and a deterministic model client.  The
workspace backend, worktree, quick test, and final pytest remain real.  It is
safe to rerun with the same report after a host-side interruption; a failed
WSL service is recorded as ``blocked`` rather than being treated as a pass.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import uuid
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from agent_runtime.linux_sandbox.environment import (  # noqa: E402
    PYTHON,
    WORKER,
    PreparedEnvironment,
    WslEnvironmentTransport,
)
from agent_runtime.linux_sandbox.task_context import SandboxEnvironmentIdentity  # noqa: E402
from agent_runtime.linux_sandbox.workspace_backend import (  # noqa: E402
    GuardedWindowsWorkspaceBackend,
)
from agent_runtime.providers.clients import FakeModelClient  # noqa: E402
from src.repair_cli import run_repair_cli  # noqa: E402
from src.repair_factory import RequiredVerifierError, wire_orchestrator  # noqa: E402

RETAINED_RELEASE = {
    "release_id": "83ecffc0a46c7644ce9c7b8255a2a9282743c8bcdd97d2e2eaf705b3014cc31c",
    "distribution": "FixLoop-Sandbox-s6-v1-83ecffc0a46c",
    "runtime_digest": "ffa81db422d58c3d21fa0674ff9c3465dd54bdf66bd03ff6ad4237d8529304af",
    "runtime_version": "s6-v1",
    "distribution_version": "26.04",
}


class RetainedEnvironment:
    """Health-check a previously accepted product-owned WSL release."""

    def __init__(self):
        self.transport = WslEnvironmentTransport()

    def ensure(self, *, package_proxy: str = "") -> PreparedEnvironment:
        if package_proxy:
            raise ValueError("S7 probe does not permit a package proxy")
        raw = self.transport.execute(
            RETAINED_RELEASE["distribution"],
            [PYTHON, "-I", "-B", WORKER, "health"],
            user="fixloop",
            timeout=90,
        )
        health = json.loads(raw)
        checks = health.get("checks", {})
        expected = {
            "unprivileged_namespaces",
            "network_namespace",
            "tmpfs_limit",
            "clean_environment",
            "control_paths_hidden",
            "fuse_device",
        }
        if health.get("runtime_digest") != RETAINED_RELEASE["runtime_digest"]:
            raise ValueError("retained runtime digest mismatch")
        if health.get("release_id") != RETAINED_RELEASE["release_id"]:
            raise ValueError("retained release identity mismatch")
        if set(checks) != expected or any(value is not True for value in checks.values()):
            raise ValueError("retained environment health failed")
        identity = SandboxEnvironmentIdentity(
            RETAINED_RELEASE["distribution"],
            RETAINED_RELEASE["distribution_version"],
            RETAINED_RELEASE["runtime_version"],
            RETAINED_RELEASE["runtime_digest"],
        )
        return PreparedEnvironment(
            identity,
            RETAINED_RELEASE["release_id"],
            "retained-existing-release",
            health,
        )


def git(root: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", "-C", str(root), *args],
        check=check,
        capture_output=True,
        text=True,
        timeout=30,
    )


def fixture(root: Path) -> tuple[Path, str]:
    repo = root / "repo"
    repo.mkdir(parents=True)
    (repo / "value.py").write_text("def answer():\n    return 1\n", encoding="utf-8")
    (repo / "test_value.py").write_text(
        "from value import answer\n\n"
        "def test_answer():\n    assert answer() == 2\n",
        encoding="utf-8",
    )
    git(repo, "init")
    git(repo, "config", "user.email", "s7-probe@example.invalid")
    git(repo, "config", "user.name", "FixLoop S7 Probe")
    git(repo, "add", ".")
    git(repo, "commit", "-m", "S7 repair fixture")
    return repo, git(repo, "rev-parse", "HEAD").stdout.strip()


def scripted_client() -> FakeModelClient:
    # This is the same deterministic tool conversation used by the public
    # repair-chain tests.  Only model responses are scripted; all IO and
    # verification are performed by the real runtime.
    return FakeModelClient(
        [
            '{"conclusion":"Read value.py, fix answer(), then verify the existing test."}',
            '<tool>{"name":"read_file","args":{"path":"value.py"}}</tool>',
            '<tool>{"name":"patch_file","args":{"path":"value.py",'
            '"old_text":"return 1","new_text":"return 2"}}</tool>',
            '<tool>{"name":"quick_test","args":{"nodeid":"test_value.py::test_answer"}}</tool>',
            "<final>Applied the requested fix.</final>",
        ]
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workspace", type=Path, default=Path.cwd())
    parser.add_argument("--report", type=Path, required=True)
    parser.add_argument("--state-root", type=Path)
    args = parser.parse_args()

    workspace = args.workspace.resolve(strict=True)
    report = {
        "status": "running",
        "checks": {},
        "environment": RETAINED_RELEASE,
        "workspace": str(workspace),
    }

    def save() -> None:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")

    def check(name: str, value: bool) -> None:
        report["checks"][name] = bool(value)
        save()
        if not value:
            raise AssertionError(name)

    try:
        fixture_root = workspace / ".tmp" / ("s7-live-" + uuid.uuid4().hex)
        repo, base = fixture(fixture_root)
        report["fixture"] = str(fixture_root)
        report["base_commit"] = base

        # The probe is explicit about its external state so a restricted
        # runner cannot silently fall back to an in-checkout .agent directory.
        if args.state_root:
            os.environ["FIXLOOP_STATE_ROOT"] = str(args.state_root.resolve())
        os.environ["FIXLOOP_USE_WORKTREE"] = "1"
        client = scripted_client()
        cli_args = SimpleNamespace(
            repo=str(repo),
            output=str(fixture_root / "delivery"),
            issue="Fix value.py: answer() returns 1 but should return 2.",
            issue_file=None,
            ref=None,
            resume_repair=None,
            skip_verify=False,
            dry_run=False,
            execution_backend="wsl_bwrap",
            execution_tier="auto",
            require_sandbox=False,
            code_exploration_mode="text",
            pylsp_path=None,
        )
        # The CLI checks provider configuration before invoking execute. This
        # probe's client is deterministic and never makes a provider request.
        os.environ.setdefault("DEEPSEEK_API_KEY", "s7-scripted-client-no-network")
        completed = {}
        calls = []

        class RecordingBackend(GuardedWindowsWorkspaceBackend):
            def execute(self, request):
                result = super().execute(request)
                calls.append(
                    {
                        "call_id": request.call_id,
                        "operation": request.operation,
                        "argv": list(request.argv),
                        "execution_status": result.execution_status,
                        "exit_code": result.exit_code,
                        "cleanup": result.cleanup,
                    }
                )
                return result

        def execute(repo_path, cancel_token, workspace_ready):
            orch = wire_orchestrator(
                client,
                repo_path,
                execution_backend="wsl_bwrap",
                sandbox_environment_manager=RetainedEnvironment(),
                skip_verify=False,
                dry_run=False,
                code_exploration_mode="text",
            )
            completed["orchestrator"] = orch
            check(
                "factory_uses_sandbox_context",
                orch._sandbox_context is orch.patcher.tool_context,
            )
            check(
                "no_host_or_docker_verifier",
                orch.verifier is None and not orch.use_pytest_verify,
            )
            orch._sandbox_backend_factory = RecordingBackend
            state = orch.repair(
                cli_args.issue,
                max_retries=1,
                repair_timeout_s=420,
                run_id="s7-live-" + uuid.uuid4().hex[:12],
                cancel_token=cancel_token,
                workspace_ready=workspace_ready,
            )
            completed["state"] = state
            return state

        report["cli_exit_code"] = run_repair_cli(cli_args, execute)
        check("cli_exit_success", report["cli_exit_code"] == 0)
        orch, state = completed["orchestrator"], completed["state"]
        delivery = Path(cli_args.output)
        cli_result = json.loads((delivery / "result.json").read_text(encoding="utf-8"))
        report["delivery"] = str(delivery)
        check("cli_report_fixed", cli_result["status"] == "fixed")
        check("cli_patch_exported", bool((delivery / "patch.diff").read_bytes()))
        check(
            "cli_patch_applies_to_source",
            git(repo, "apply", "--check", str(delivery / "patch.diff"), check=False).returncode
            == 0,
        )
        binding_wire = state.node_timings.get("sandbox_task_binding", {})
        worktree = Path(binding_wire["mapping"]["host_root"])
        backend = orch._sandbox_context.sandbox_backend
        report.update(
            {
                "repair_status": state.status,
                "coordination_status": state.control.coordination_status,
                "repair_run_id": state.repair_run_id,
                "binding": binding_wire,
                "verification": state.verification_result.to_dict()
                if state.verification_result is not None
                else None,
            }
        )
        check("repair_finished_successfully", state.status == "fixed")
        check("coordination_released", state.control.coordination_status == "released")
        check("task_binding_created", bool(binding_wire))
        check(
            "single_backend_identity",
            binding_wire.get("environment", {}).get("distribution")
            == RETAINED_RELEASE["distribution"]
            and binding_wire.get("environment", {}).get("runtime_digest")
            == RETAINED_RELEASE["runtime_digest"],
        )
        check("worktree_is_task_owned", binding_wire.get("workspace_ownership") == "task")
        check(
            "patch_applied_in_worktree",
            (worktree / "value.py").read_text() == "def answer():\n    return 2\n",
        )
        check(
            "source_repository_unchanged",
            (repo / "value.py").read_text() == "def answer():\n    return 1\n",
        )
        check(
            "final_pytest_passed",
            bool(state.verification_result and state.verification_result.all_passed),
        )
        check("backend_closed_after_repair", bool(getattr(backend, "_closed", False)))
        check(
            "backend_context_identity_shared",
            backend.task_binding.task_id == state.repair_run_id,
        )
        report["model_calls"] = client.session_usage["calls"]
        report["executions"] = calls
        pytest_calls = [call for call in calls if call["operation"] == "pytest"]
        quick_calls = [
            call for call in pytest_calls if "test_value.py::test_answer" in call["argv"]
        ]
        check(
            "baseline_pytest_reproduced_failure",
            bool(pytest_calls) and pytest_calls[0]["exit_code"] == 1,
        )
        check(
            "quick_and_final_pytest_executed",
            len(pytest_calls) >= 3 and bool(quick_calls) and pytest_calls[-1] not in quick_calls,
        )
        check(
            "quick_and_final_pytest_passed",
            all(call["exit_code"] == 0 for call in quick_calls)
            and pytest_calls[-1]["exit_code"] == 0,
        )
        receipts = [backend.inspect_receipt(call["call_id"])["result"] for call in pytest_calls]
        report["pytest_receipts"] = receipts
        check(
            "pytest_receipts_confirmed",
            all(
                receipt["exit_code"] == call["exit_code"]
                and receipt["cleanup"] == "confirmed"
                and receipt["execution_status"] == "completed"
                for call, receipt in zip(pytest_calls, receipts, strict=True)
            ),
        )
        report["status"] = "passed"
    except (RequiredVerifierError, OSError, ValueError, RuntimeError, AssertionError) as exc:
        report.update(
            {
                "status": (
                    "blocked"
                    if isinstance(exc, RequiredVerifierError | OSError)
                    else "failed"
                ),
                "error_type": type(exc).__name__,
                "error": str(exc),
            }
        )
    finally:
        save()

    print(json.dumps(report, ensure_ascii=True, indent=2))
    return 0 if report["status"] == "passed" else 2


if __name__ == "__main__":
    raise SystemExit(main())
