"""S5 real repository/worktree acceptance; production remains gated."""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
import textwrap
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from agent_runtime.linux_sandbox.environment import (  # noqa: E402
    EnvironmentError,
    SandboxEnvironmentManager,
)
from agent_runtime.linux_sandbox.permissions import (  # noqa: E402
    PermissionScope,
    PermissionService,
    permission_service,
)
from agent_runtime.linux_sandbox.routing import execute_sandbox  # noqa: E402
from agent_runtime.linux_sandbox.task_context import (  # noqa: E402
    SandboxTaskBinding,
    SandboxTaskSpec,
)
from agent_runtime.linux_sandbox.workspace_backend import (  # noqa: E402
    GuardedWindowsWorkspaceBackend,
)
from agent_runtime.linux_sandbox.workspace_routing import (  # noqa: E402
    execute_canonical_tool,
    execute_workspace_tool,
)
from agent_runtime.tool_context import ToolContext  # noqa: E402
from scripts.wsl_sandbox_s4_probe import git, wheel  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workspace", type=Path, default=Path.cwd())
    parser.add_argument("--home", type=Path)
    parser.add_argument("--resume-final", action="store_true")
    parser.add_argument("--report", type=Path, required=True)
    args = parser.parse_args()
    report = {"status": "running", "checks": {}, "calls": []}
    previous = None
    if args.report.exists():
        previous = json.loads(args.report.read_text(encoding="utf-8"))
        report["previous_runs"] = [
            *previous.get("previous_runs", []),
            {k: v for k, v in previous.items() if k != "previous_runs"},
        ]

    if args.resume_final:
        if previous is None:
            raise ValueError("resume requires a retained report")
        report = {**previous, "status": "running"}
        report["previous_runs"] = [
            *previous.get("previous_runs", []),
            {k: v for k, v in previous.items() if k != "previous_runs"},
        ]
        report.pop("error", None)

    def save():
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")

    def check(name, value):
        report["checks"][name] = bool(value)
        print(name + ": " + str(bool(value)), flush=True)
        save()
        if not value:
            raise AssertionError(name)

    def record(label, result, backend):
        wire = (
            result.to_wire()
            if hasattr(result, "to_wire")
            else {
                "status": str(result.status),
                "error_code": result.error_code,
                "content": result.content,
                "metadata": result.metadata,
                "changed_files": result.changed_files,
                "receipt": result.receipt,
            }
        )
        receipt_id = wire.get("receipt_id") or wire.get("metadata", {}).get("sandbox_receipt_id")
        report["calls"].append(
            {
                "label": label,
                "result": wire,
                "receipt": backend.inspect_receipt(receipt_id) if receipt_id else None,
            }
        )
        save()
        return result

    try:
        workspace = args.workspace.resolve(strict=True)
        environment = SandboxEnvironmentManager(workspace, home=args.home).ensure()
        if args.resume_final:
            if (
                report.get("release_id") != environment.release_id
                or report.get("runtime_digest") != environment.identity.runtime_digest
                or report.get("distribution") != environment.identity.distribution
                or not all(
                    report["checks"].get(label + "_pytest_auto_allowed")
                    for label in ("repository", "worktree")
                )
            ):
                raise ValueError("resume requires completed approval checks on this release")
            fixture = Path(report["fixture"])
            repository, worktree = fixture / "repo", fixture / "worktree"
            dep = "fixloop_s5_dep-1.0-py3-none-any.whl"
        else:
            fixture = workspace / ".tmp" / ("s5-live-" + uuid.uuid4().hex)
            repository, worktree = fixture / "repo", fixture / "worktree"
            repository.mkdir(parents=True)
            (repository / "value.py").write_text("value = 1\n", encoding="utf-8")
            (repository / "test_value.py").write_text(
                "from value import value\ndef test_value(): assert value == 2\n", encoding="utf-8"
            )
            dep = wheel(repository, "fixloop_s5_dep", 17)
            git(repository, "init")
            git(repository, "add", ".")
            git(
                repository,
                "-c",
                "user.name=FixLoop Probe",
                "-c",
                "user.email=probe@example.invalid",
                "commit",
                "-m",
                "S5 acceptance fixture",
            )
            git(repository, "worktree", "add", "-b", "s5-probe", str(worktree))
        report.update(
            release_id=environment.release_id,
            distribution=environment.identity.distribution,
            runtime_digest=environment.identity.runtime_digest,
            fixture=str(fixture),
        )
        scope = PermissionScope(
            write_paths=(),
            capabilities=("workspace_read", "python", "pytest", "trusted_data", "trusted_control"),
        )
        for label, root in (("repository", repository), ("worktree", worktree)):
            if args.resume_final and report["checks"].get(label + "_authority_reload_final"):
                continue

            def verify(name, value):
                check(label + "_" + name, value)

            spec = SandboxTaskSpec(
                environment.identity,
                "s5-v1",
                hashlib.sha256(b"s5-v1").hexdigest(),
                str(fixture / "state"),
            )
            binding = SandboxTaskBinding.create(repository, root, "s5-" + label, spec)
            backend = GuardedWindowsWorkspaceBackend(binding)
            context = ToolContext(str(root), sandbox_backend=backend)
            context.sandbox_initial_scope = scope
            binding.attach(context)
            service = permission_service(context)

            def tool(name, arguments):
                return record(
                    label + "_" + name, execute_workspace_tool(context, name, arguments), backend
                )

            def command(argv, timeout=20):
                return record(
                    label + "_command",
                    execute_sandbox(context, "command", tuple(argv), timeout),
                    backend,
                )

            if not (args.resume_final and report["checks"].get(label + "_pytest_auto_allowed")):
                read = tool("read_file", {"path": "value.py"})
                verify(
                    "read_auto_allowed", read.ok and bool(read.metadata.get("sandbox_receipt_id"))
                )
                before = backend.store.current()
                edit = {"path": "value.py", "content": "value = 2\n"}
                waiting = tool("write_file", edit)
                proposal = service.snapshot()["pending"]
                verify(
                    "approval_not_started",
                    waiting.error_code == "approval_required"
                    and waiting.metadata["sandbox_mutation_status"] == "not_started"
                    and not waiting.metadata["sandbox_receipt_id"]
                    and backend.store.current() == before
                    and service.snapshot()["tickets"] == {}
                    and (root / "value.py").read_text() == "value = 1\n",
                )
                reloaded = PermissionService.for_binding(binding)
                verify("pending_reloaded", reloaded.snapshot()["pending"] == proposal)
                reloaded.resolve(proposal["id"], allow=True)
                verify(
                    "approval_does_not_grant_or_execute",
                    service.snapshot()["scope"]["write_paths"] == []
                    and backend.store.current() == before,
                )
                resumed = tool("write_file", edit)
                verify(
                    "approved_exact_write",
                    resumed.ok and (root / "value.py").read_text() == "value = 2\n",
                )
                state = service.snapshot()
                verify(
                    "grant_committed_on_consume",
                    state["pending"]["status"] == "consumed"
                    and state["scope"]["write_paths"] == ["value.py"]
                    and state["tickets"] == {},
                )

                before = backend.store.current()
                denied_args = {"path": "denied.py", "content": "value = 3\n"}
                denied = tool("write_file", denied_args)
                denied_id = service.snapshot()["pending"]["id"]
                service.resolve(denied_id, allow=False)
                denied_retry = tool("write_file", denied_args)
                verify(
                    "denied_never_started",
                    denied.error_code == "approval_required"
                    and denied_retry.error_code == "approval_denied"
                    and backend.store.current() == before
                    and not (root / "denied.py").exists(),
                )

                for mode in ("cancel", "revoke", "changed_args", "changed_input"):
                    before = backend.store.current()
                    target = mode + ".py"
                    action = {"path": target, "content": "value = 4\n"}
                    pending = tool("write_file", action)
                    verify(mode + "_pending", pending.error_code == "approval_required")
                    pending_id = service.snapshot()["pending"]["id"]
                    if mode == "cancel":
                        service.cancel_pending(pending_id)
                    else:
                        service.resolve(pending_id, allow=True)
                    if mode == "revoke":
                        service.replace_scope(PermissionScope(**service.snapshot()["scope"]))
                    if mode == "changed_args":
                        action = {**action, "content": "changed invocation\n"}
                    if mode == "changed_input":
                        (root / target).write_text("user edit\n")
                    rejected = tool("write_file", action)
                    verify(
                        mode + "_rejected",
                        not rejected.ok
                        and backend.store.current() == before
                        and target not in service.snapshot()["scope"]["write_paths"]
                        and (
                            (root / target).read_text() == "user edit\n"
                            if mode == "changed_input"
                            else not (root / target).exists()
                        ),
                    )
                    service.cancel_pending()

                install_argv = ("/toolchain/bin/python", "-m", "pip", "install", dep)
                before = backend.store.current()
                install = command(install_argv, 40)
                verify(
                    "install_pending",
                    install.error_code == "approval_required"
                    and backend.store.current() == before
                    and service.snapshot()["tickets"] == {},
                )
                install_id = service.snapshot()["pending"]["id"]
                service.resolve(install_id, allow=True)
                wheel(root, "fixloop_s5_dep", 23)
                changed_wheel = command(install_argv, 40)
                verify(
                    "changed_wheel_rejected",
                    changed_wheel.error_code == "approval_pending"
                    and backend.store.current() == before
                    and "offline_install" not in service.snapshot()["scope"]["capabilities"],
                )
                service.cancel_pending()
                wheel(root, "fixloop_s5_dep", 17)
                install = command(install_argv, 40)
                verify("new_install_pending", install.error_code == "approval_required")
                service.resolve(service.snapshot()["pending"]["id"], allow=True)
                installed = command(install_argv, 40)
                verify(
                    "approved_install",
                    installed.execution_status == "completed" and installed.exit_code == 0,
                )
                imported = command(
                    (
                        "/toolchain/bin/python",
                        "-c",
                        "import fixloop_s5_dep; assert fixloop_s5_dep.VALUE == 17; print('ok')",
                    )
                )
                verify("dependency_survives_call", imported.exit_code == 0)
                tested = command(("/toolchain/bin/python", "-m", "pytest", "test_value.py"))
                verify(
                    "pytest_auto_allowed",
                    tested.exit_code == 0 and tested.execution_status == "completed",
                )
            if not (
                args.resume_final
                and report["checks"].get(label + "_approval_preserves_os_isolation")
            ):
                isolated = command(
                    (
                        "/toolchain/bin/python",
                        "-c",
                        textwrap.dedent("""
    import os, socket
    from pathlib import Path
    assert not Path('/mnt/c').exists() and not Path('/opt/fixloop').exists()
    assert not any(key in os.environ for key in ('WSL_INTEROP', 'WSLENV'))
    for target in ('/workspace/forbidden.py', '/task/deps/forbidden.py'):
        try: Path(target).write_text('no')
        except OSError: pass
        else: raise AssertionError('writable command mount')
    with socket.socket() as stream:
        stream.settimeout(1)
        try: stream.connect(('1.1.1.1', 443))
        except OSError: pass
        else: raise AssertionError('network available')
    print('isolated')
    """),
                    )
                )
                verify(
                    "approval_preserves_os_isolation",
                    isolated.exit_code == 0
                    and isolated.execution_status == "completed"
                    and not (root / "forbidden.py").exists(),
                )

            before = backend.store.current()
            for name, argv in (
                ("shell", ("/bin/sh", "-c", "echo unsafe")),
                (
                    "network_install",
                    (
                        "/toolchain/bin/python",
                        "-m",
                        "pip",
                        "install",
                        "https://example.invalid/demo.whl",
                    ),
                ),
            ):
                blocked = command(argv)
                verify(
                    name + "_denied",
                    blocked.error_code in {"policy_denied", "workspace_mapping_rejected"}
                    and backend.store.current() == before,
                )
            trusted = record(
                label + "_trusted",
                execute_canonical_tool(
                    context, "stack_parse", {"traceback": "Traceback (most recent call last):\n"}
                ),
                backend,
            )
            verify(
                "trusted_data_audited",
                trusted.ok
                and trusted.metadata["execution_tier"] == "trusted_host"
                and backend.store.current() == before,
            )
            audit = service.snapshot()["audit"]
            proposal = {
                "id": next(e["approval_id"] for e in audit if e["event"] == "grant_committed")
            }
            denied_id = next(e["approval_id"] for e in audit if e["event"] == "denied")
            events = [
                event["event"] for event in audit if event.get("approval_id") == proposal["id"]
            ]
            verify("approval_audit_paired", events == ["pending", "approved", "grant_committed"])
            verify(
                "denial_audit_paired",
                [e["event"] for e in audit if e.get("approval_id") == denied_id]
                == ["pending", "denied"],
            )
            starts = [e for e in audit if e["event"] == "started"]
            outcomes = [e for e in audit if e["event"] == "outcome"]
            verify(
                "execution_audit_paired",
                starts
                and all(
                    any(o["authorization_id"] == s["authorization_id"] for o in outcomes)
                    for s in starts
                ),
            )
            verify(
                "authority_reload_final",
                PermissionService.for_binding(binding).snapshot() == service.snapshot(),
            )
            report.setdefault("authorities", {})[label] = service.snapshot()
        report["status"] = "passed"
        save()
        print(f"S5 acceptance: {len(report['checks'])} checks passed")
        return 0
    except EnvironmentError as exc:
        report.update(status="blocked", error=exc.code, guidance=list(exc.guidance))
    except Exception as exc:
        report.update(status="failed", error=type(exc).__name__ + ": " + str(exc))
    save()
    print(
        json.dumps(
            {k: v for k, v in report.items() if k not in {"calls", "previous_runs"}}, indent=2
        )
    )
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
