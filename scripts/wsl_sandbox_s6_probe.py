"""S6 acceptance on the fixed Windows -> WSL -> cgroup -> bwrap path."""

from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from agent_runtime.linux_sandbox.environment import (  # noqa: E402
    EnvironmentError,
    SandboxEnvironmentManager,
)
from agent_runtime.linux_sandbox.permissions import PermissionScope  # noqa: E402
from agent_runtime.linux_sandbox.resource_supervisor import execution_key  # noqa: E402
from agent_runtime.linux_sandbox.routing import execute_sandbox  # noqa: E402
from agent_runtime.linux_sandbox.task_context import (  # noqa: E402
    SandboxEnvironmentIdentity,
    SandboxTaskBinding,
    SandboxTaskSpec,
)
from agent_runtime.linux_sandbox.task_dependencies import task_key  # noqa: E402
from agent_runtime.linux_sandbox.workspace_backend import (  # noqa: E402
    GuardedWindowsWorkspaceBackend,
)
from agent_runtime.linux_sandbox.workspace_routing import execute_workspace_tool  # noqa: E402
from agent_runtime.linux_sandbox.workspace_state import (  # noqa: E402
    restore_workspace,
    workspace_snapshot,
)
from agent_runtime.tool_context import ToolContext  # noqa: E402
from scripts.wsl_sandbox_s4_probe import git, wheel  # noqa: E402


def context_for(binding):
    backend = GuardedWindowsWorkspaceBackend(binding)
    context = ToolContext(binding.mapping.host_root, sandbox_backend=backend)
    binding.attach(context)
    return context


def python(context, code, timeout=20):
    return execute_sandbox(context, "command", ("/toolchain/bin/python", "-c", code), timeout)


def wait_until(callback, timeout=20):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = callback()
        if value:
            return value
        time.sleep(0.1)
    raise TimeoutError("probe condition did not become true")


def child(binding_path):
    raw = json.loads(binding_path.read_text("utf-8"))
    spec = SandboxTaskSpec(
        SandboxEnvironmentIdentity(**raw["environment"]),
        raw["policy_version"],
        raw["policy_digest"],
        str(Path(raw["state_root"]).parent.parent),
    )
    binding = SandboxTaskBinding.load(raw["repository_root"], raw["task_id"], spec)
    context = context_for(binding)
    python(context, "import time; time.sleep(90)", 120)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workspace", type=Path, default=Path.cwd())
    parser.add_argument("--report", type=Path)
    parser.add_argument("--child", type=Path)
    parser.add_argument("--retire-environment", action="store_true")
    parser.add_argument("--resume-lifecycle", action="store_true")
    args = parser.parse_args()
    if args.child:
        child(args.child)
        return 0
    if args.report is None:
        parser.error("--report is required")
    report = {"status": "running", "checks": {}, "calls": []}
    if args.resume_lifecycle:
        previous = json.loads(args.report.read_text("utf-8"))
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

    def record(name, result):
        report["calls"].append({"label": name, "result": result.to_wire()})
        save()
        return result

    try:
        workspace = args.workspace.resolve(strict=True)
        manager = SandboxEnvironmentManager(workspace)
        environment = manager.ensure()
        if args.resume_lifecycle and report["release_id"] != environment.release_id:
            raise ValueError("resume environment mismatch")
        report.update(
            release_id=environment.release_id,
            runtime_digest=environment.identity.runtime_digest,
            distribution=environment.identity.distribution,
        )
        fixture = (
            Path(report["fixture"])
            if args.resume_lifecycle
            else workspace / ".tmp" / ("s6-live-" + uuid.uuid4().hex)
        )
        repository, worktree = fixture / "repo", fixture / "worktree"
        if not args.resume_lifecycle:
            repository.mkdir(parents=True)
            (repository / "value.py").write_text("value = 1\n", encoding="utf-8")
            package = wheel(repository, "fixloop_s6_dep", 61)
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
                "S6 acceptance fixture",
            )
            git(repository, "worktree", "add", "-b", "s6-probe", str(worktree))
        report["fixture"] = str(fixture)
        spec = SandboxTaskSpec(
            environment.identity,
            "s6-v1",
            hashlib.sha256(b"s6-v1").hexdigest(),
            str(fixture / "state"),
        )
        contexts = []

        def bind(root, name):
            path = fixture / "state" / "tasks" / name / "sandbox-task.json"
            if path.exists():
                binding = SandboxTaskBinding.load(repository, name, spec)
            else:
                binding = SandboxTaskBinding.create(repository, root, name, spec)
                binding.save()
            context = context_for(binding)
            contexts.append(context)
            return context

        def native(code):
            return json.loads(
                manager.transport.execute(
                    environment.identity.distribution,
                    ["/usr/bin/python3.14", "-I", "-B", "-c", code],
                    user="root",
                    timeout=20,
                )
            )

        def execution_snapshot(context):
            current = context.sandbox_backend.store.current()
            if not current or current["state"] != "running":
                return None
            key = execution_key(
                SimpleNamespace(
                    **{
                        name: current[name]
                        for name in ("workspace_id", "task_id", "run_id", "call_id")
                    }
                ),
                environment.identity.runtime_digest,
            )
            data = native(
                "import json; from pathlib import Path; "
                f"p=Path('/sys/fs/cgroup/fixloop-sandbox/{key}'); "
                "print(json.dumps({n:(p/n).read_text().strip() for n in "
                "['memory.max','memory.swap.max','cpu.max','pids.max','cgroup.procs']} "
                "if p.exists() else {}))"
            )
            return {"key": key, "call_id": current["call_id"], **data} if data else None

        if args.resume_lifecycle:
            bind(repository, "s6-repository")
            bind(worktree, "s6-worktree")
        for label, root in (
            () if args.resume_lifecycle else (("repository", repository), ("worktree", worktree))
        ):
            context = bind(root, "s6-" + label)
            backend = context.sandbox_backend
            result = record(
                label + "_read",
                python(context, "from pathlib import Path; print(Path('value.py').read_text())"),
            )
            check(
                label + "_guarded_read",
                result.cleanup == "confirmed" and "value = 1" in result.stdout_excerpt,
            )
            snapshot = workspace_snapshot(context)
            check(
                label + "_read_before_write",
                execute_workspace_tool(context, "read_file", {"path": "value.py"}).ok,
            )
            edit = execute_workspace_tool(
                context, "write_file", {"path": "value.py", "content": "value = 2\n"}
            )
            check(label + "_write_committed", edit.ok)
            restarted = context_for(context.sandbox_task_binding)
            check(
                label + "_persistent_ownership",
                "value.py" in restarted.sandbox_backend.owned_versions(),
            )
            restore_workspace(restarted, snapshot)
            check(
                label + "_rollback_after_restart", (root / "value.py").read_text() == "value = 1\n"
            )
            execute_workspace_tool(restarted, "read_file", {"path": "value.py"})
            execute_workspace_tool(
                restarted, "write_file", {"path": "value.py", "content": "value = 3\n"}
            )
            (root / "value.py").write_text("# user edit\nvalue = 4\n")
            try:
                restore_workspace(restarted, snapshot)
                protected = False
            except ValueError as exc:
                protected = "external edit" in str(exc)
            check(label + "_external_edit_protected", protected)
            backend.permissions.replace_scope(
                PermissionScope(
                    install_paths=(package,),
                    capabilities=(*PermissionScope().capabilities, "offline_install"),
                )
            )
            result = record(
                label + "_install",
                execute_sandbox(
                    context,
                    "command",
                    ("/toolchain/bin/python", "-m", "pip", "install", package),
                    30,
                ),
            )
            check(label + "_install", result.exit_code == 0 and result.cleanup == "confirmed")
            result = record(
                label + "_import",
                python(restarted, "import fixloop_s6_dep; print(fixloop_s6_dep.VALUE)"),
            )
            check(label + "_deps_survive_backend_restart", "61" in result.stdout_excerpt)

        main_context, other_context = contexts
        main_backend = main_context.sandbox_backend
        if not args.resume_lifecycle:
            result = record("memory", python(main_context, "data=bytearray(768*1024*1024)"))
            check(
                "memory_cgroup_kills_and_confirms",
                result.cleanup == "confirmed"
                and result.tool_result.get("error_code") == "resource_limit_exceeded",
            )
            fork_code = (
                "import os,time\npids=[]\ntry:\n while True:\n  pid=os.fork()\n"
                "  if pid==0: time.sleep(30); os._exit(0)\n  pids.append(pid)\n"
                "except OSError as e:\n print('PIDS_LIMIT',len(pids),e.errno,flush=True)\n"
            )
            result = record("fork", python(main_context, fork_code))
            check(
                "fork_limit_and_descendant_cleanup",
                result.cleanup == "confirmed" and "PIDS_LIMIT" in result.stdout_excerpt,
            )
            result = record("cpu_timeout", python(main_context, "while True: pass", 1))
            check(
                "cpu_wall_deadline_and_cleanup",
                result.cleanup == "confirmed" and result.execution_status == "cancelled",
            )
            result = record(
                "tmp",
                python(
                    main_context,
                    "from pathlib import Path; "
                    "Path('/tmp/one').write_bytes(b'x'*40*1024*1024); "
                    "Path('/tmp/two').write_bytes(b'x'*40*1024*1024)",
                ),
            )
            check(
                "tmp_hard_quota",
                result.cleanup == "confirmed" and "No space left" in result.stderr_excerpt,
            )
            result = record(
                "output", python(main_context, "while True: print('x'*65536,flush=True)")
            )
            check("output_bounded", result.cleanup == "confirmed" and result.output_truncated)

            results = []
            thread = threading.Thread(
                target=lambda: results.append(
                    python(
                        main_context,
                        "import os,time; p=os.fork(); "
                        "os.setsid() if p==0 else None; time.sleep(90)",
                        120,
                    )
                )
            )
            thread.start()
            live = wait_until(lambda: execution_snapshot(main_context))
            report["live_resource_profile"] = live
            check(
                "kernel_resource_profile",
                live["memory.max"] == "536870912"
                and live["pids.max"] == "64"
                and live["cpu.max"] == "100000 100000"
                and live["memory.swap.max"] == "0",
            )
            independent = record("independent", python(other_context, "print('independent')"))
            check("other_task_survives", "independent" in independent.stdout_excerpt)
            main_backend.close()
            thread.join(timeout=15)
            check(
                "close_cancels_detached_tree",
                not thread.is_alive()
                and results
                and results[0].cleanup == "confirmed"
                and results[0].execution_status == "cancelled",
            )
            check("close_idempotent", main_backend.close()["state"] == "terminal")

        crashed = bind(repository, "s6-crash")
        if not report["checks"].get("fresh_execution_after_recovery"):
            crash_log = (fixture / "crash-child.log").open("wb")
            process = subprocess.Popen(
                [
                    sys.executable,
                    str(Path(__file__).resolve()),
                    "--child",
                    str(Path(crashed.state_root) / "sandbox-task.json"),
                ],
                stdout=crash_log,
                stderr=crash_log,
            )
            try:

                def child_started():
                    if process.poll() is not None:
                        raise RuntimeError((fixture / "crash-child.log").read_text("utf-8"))
                    return execution_snapshot(crashed)

                crash_live = wait_until(child_started, timeout=30)
                process.kill()
                process.wait(timeout=10)
                recovered = crashed.sandbox_backend.recover()
                report["crash_recovery"] = recovered
                check(
                    "host_crash_recovered_without_replay",
                    recovered["state"] == "terminal"
                    and recovered["result"]["cleanup"] == "confirmed"
                    and recovered["result"]["mutation_status"] == "not_started",
                )
                check(
                    "crashed_cgroup_removed",
                    not native(
                        "import json; from pathlib import Path; "
                        f"print(json.dumps(Path('/sys/fs/cgroup/fixloop-sandbox/{crash_live['key']}').exists()))"
                    ),
                )
                result = record("after_crash", python(crashed, "print('recovered')"))
                check("fresh_execution_after_recovery", "recovered" in result.stdout_excerpt)
            finally:
                if process.poll() is None:
                    process.kill()
                    process.wait(timeout=10)
                crash_log.close()

        killed = bind(repository, "s6-supervisor")
        killed_results = []
        killed_thread = threading.Thread(
            target=lambda: killed_results.append(
                python(
                    killed,
                    "import os,time; tag='FIXLOOP_S6_DOUBLE_FORK'\n"
                    "if os.fork()==0:\n os.setsid()\n"
                    " if os.fork()==0: time.sleep(90)\n os._exit(0)\n"
                    "time.sleep(90)",
                    120,
                )
            )
        )
        killed_thread.start()
        killed_live = wait_until(lambda: execution_snapshot(killed))
        native_prefix = (
            "import sys,types; sys.path.insert(0,'/opt/fixloop/runtime'); "
            "m=types.ModuleType('agent_runtime'); "
            "m.__path__=['/opt/fixloop/runtime/agent_runtime']; sys.modules['agent_runtime']=m; "
            "from agent_runtime.linux_sandbox.receipts import _read,process_identity; "
            "from pathlib import Path; import os,json,signal; "
        )
        wait_until(
            lambda: native(
                native_prefix
                + f"p=Path('/sys/fs/cgroup/fixloop-sandbox/{killed_live['key']}/cgroup.procs'); "
                + "print(json.dumps(sum(b'FIXLOOP_S6_DOUBLE_FORK' in "
                "Path('/proc/'+pid+'/cmdline').read_bytes() "
                "for pid in p.read_text().split())>=2))"
            )
        )
        identity = native(
            native_prefix
            + f"r=_read(Path('/var/lib/fixloop/executions/{killed_live['key']}.json')); "
            + "i=r['supervisor_identity']; assert process_identity(i['pid'])==i; "
            "assert b'/resource_supervisor.py' in "
            "Path('/proc/'+str(i['pid'])+'/cmdline').read_bytes(); "
            "os.kill(i['pid'],signal.SIGKILL); print(json.dumps(i))"
        )
        report["killed_supervisor_identity"] = identity
        killed_thread.join(timeout=15)
        check(
            "supervisor_death_does_not_claim_cleanup",
            not killed_thread.is_alive()
            and killed_results
            and killed_results[0].execution_status == "uncertain"
            and killed_results[0].cleanup != "confirmed",
        )
        recovered = killed.sandbox_backend.recover()
        report["supervisor_recovery"] = recovered
        check(
            "supervisor_death_reconciles_double_fork_tree",
            recovered["result"]["cleanup"] == "confirmed"
            and recovered["result"]["mutation_status"] == "not_started"
            and not native(
                "import json; from pathlib import Path; "
                f"print(json.dumps(Path('/sys/fs/cgroup/fixloop-sandbox/{killed_live['key']}').exists()))"
            ),
        )

        pending = bind(repository, "s6-pending")
        pending.sandbox_backend.permissions.replace_scope(
            PermissionScope(capabilities=("workspace_read",))
        )
        result = python(pending, "print('approval')")
        check(
            "approval_waits_without_execution",
            result.error_code == "approval_required"
            and pending.sandbox_backend.store.current() is None,
        )
        try:
            pending.sandbox_backend.retire()
            blocked = False
        except ValueError as exc:
            blocked = "permission pending" in str(exc)
        check("pending_approval_blocks_task_gc", blocked)
        try:
            manager.retire()
            blocked = False
        except EnvironmentError as exc:
            blocked = exc.code == "environment_in_use"
        check("durable_task_pins_block_environment_gc", blocked)
        pending.sandbox_backend.permissions.cancel_pending()

        for context in contexts:
            binding = context.sandbox_task_binding
            backend = context.sandbox_backend
            result = backend.retire()
            check(
                binding.task_id + "_retired",
                result["state"] == "retired" and backend.retire() == result,
            )
            key = task_key(
                SimpleNamespace(
                    workspace_id=binding.mapping.workspace_id,
                    task_id=binding.task_id,
                    run_id=binding.run_id,
                ),
                {"runtime_digest": environment.identity.runtime_digest},
            )
            check(
                binding.task_id + "_only_dependencies_collected",
                native(
                    "import json; from pathlib import Path; "
                    f"p=Path('/home/fixloop/tasks/{key}'); "
                    "print(json.dumps(not (p/'deps').exists() and (p/'retired.json').exists()))"
                ),
            )
            check(
                binding.task_id + "_user_state_retained",
                Path(binding.state_root).exists()
                and Path(binding.mapping.host_root).exists()
                and (Path(binding.state_root) / "sandbox-task.json").exists(),
            )

        clean = native(
            "import json; from pathlib import Path; "
            "print(json.dumps({'cgroups':[p.name for p in "
            "Path('/sys/fs/cgroup/fixloop-sandbox').iterdir() if p.is_dir()],"
            "'scratch':[p.name for p in Path('/var/lib/fixloop/scratch').iterdir()]}))"
        )
        report["native_remaining"] = clean
        check(
            "no_execution_cgroups_or_temporary_mount_directories",
            clean == {"cgroups": [], "scratch": []},
        )
        check(
            "worktree_and_user_edits_preserved",
            (worktree / ".git").exists() and "user edit" in (worktree / "value.py").read_text(),
        )
        if args.retire_environment:
            check("environment_retired_after_all_tasks", manager.retire()["stage"] == "retired")
            check("environment_retirement_idempotent", manager.retire()["stage"] == "retired")
        report["status"] = "passed"
    except Exception as exc:
        report.update(status="failed", error=type(exc).__name__ + ": " + str(exc))
        raise
    finally:
        save()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
