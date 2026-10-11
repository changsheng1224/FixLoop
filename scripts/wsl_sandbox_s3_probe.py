"""Explicit S3 acceptance probe; creates retained fixture repositories, never enables production."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
import textwrap
import threading
import time
import uuid
from pathlib import Path
from unittest.mock import patch as mock_patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from agent_runtime.linux_sandbox.environment import (  # noqa: E402
    SandboxEnvironmentManager,
    decode_wsl,
)
from agent_runtime.linux_sandbox.task_context import (  # noqa: E402
    SandboxTaskBinding,
    SandboxTaskSpec,
)
from agent_runtime.linux_sandbox.workspace_backend import (  # noqa: E402
    GuardedWindowsWorkspaceBackend,
)
from agent_runtime.linux_sandbox.workspace_routing import execute_workspace_tool  # noqa: E402
from agent_runtime.tool_context import ToolContext  # noqa: E402


def git(root, *args):
    subprocess.run(["git", "-C", str(root), *args], check=True, capture_output=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workspace", type=Path, default=Path.cwd())
    parser.add_argument("--report", type=Path, required=True)
    args = parser.parse_args()
    workspace = args.workspace.resolve(strict=True)
    prepared = SandboxEnvironmentManager(workspace).ensure()
    fixture = workspace / ".tmp" / ("s3-live-" + uuid.uuid4().hex)
    fixture.mkdir(parents=True)
    repository = fixture / "repo"
    repository.mkdir()
    (repository / "src").mkdir()
    (repository / "tests").mkdir()
    (repository / "src/add.py").write_text("def add(a, b):\n    return a + b\n", encoding="utf-8")
    (repository / "tests/test_add.py").write_text("def test_add():\n    assert True\n")
    (repository / "README.md").write_text("S3 acceptance repository\n")
    git(repository, "init")
    git(repository, "add", "src", "tests", "README.md")
    git(
        repository,
        "-c",
        "user.name=FixLoop Probe",
        "-c",
        "user.email=probe@example.invalid",
        "commit",
        "-m",
        "S3 acceptance fixture",
    )
    worktree = fixture / "worktree"
    git(repository, "worktree", "add", "-b", "s3-probe", str(worktree))
    checks, calls = {}, []
    report = {
        "release_id": prepared.release_id,
        "distribution": prepared.identity.distribution,
        "runtime_digest": prepared.identity.runtime_digest,
        "fixture": str(fixture),
        "checks": checks,
        "calls": calls,
    }

    def check(name, condition):
        checks[name] = bool(condition)
        print(name + ": " + str(bool(condition)), flush=True)
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
        if not condition:
            raise AssertionError(name)

    # Execute the actual S3 bwrap argv in the same private user/mount setup.
    # Only this explicit acceptance probe substitutes the fixed worker program.
    isolation_worker = textwrap.dedent("""
        import ctypes, fcntl, json, os, socket
        from pathlib import Path
        checks = {}
        checks['host_drives_hidden'] = not Path('/mnt/c').exists()
        checks['host_home_hidden'] = not Path('/home').exists()
        checks['interop_environment_absent'] = not ({'WSL_INTEROP', 'WSLENV'} & os.environ.keys())
        checks['network_routes_absent'] = len(Path('/proc/net/route').read_text().splitlines()) == 1
        try:
            socket.create_connection(('172.25.32.1', 7897), timeout=1).close()
            checks['host_network_unreachable'] = False
        except OSError:
            checks['host_network_unreachable'] = True
        try:
            Path('/workspace/new').write_text('forbidden')
            checks['workspace_view_readonly'] = False
        except OSError:
            checks['workspace_view_readonly'] = True
        try:
            Path('/executor/agent_runtime/new').write_text('forbidden')
            checks['runtime_readonly'] = False
        except OSError:
            checks['runtime_readonly'] = True
        libc = ctypes.CDLL(None, use_errno=True)
        checks['nested_user_namespace_denied'] = libc.unshare(0x10000000) == -1
        descriptors = {}
        for p in Path('/proc/self/fd').iterdir():
            try:
                descriptors[p.name] = os.readlink(p)
            except FileNotFoundError:
                continue
        checks['broker_fds_absent'] = all(
            int(fd) <= 2 or (
                target == '/usr/lib/x86_64-linux-gnu/libffi.so.8'
                and fcntl.fcntl(int(fd), fcntl.F_GETFL) & os.O_ACCMODE == os.O_RDONLY
            )
            for fd, target in descriptors.items()
        )
        if not checks['broker_fds_absent']:
            print(json.dumps({'descriptors': descriptors}), file=__import__('sys').stderr)
        print(json.dumps({'checks': checks, 'descriptors': descriptors}))
    """)
    isolation_controller = textwrap.dedent("""
        import json, socket, subprocess, sys, tempfile, types
        from pathlib import Path
        sys.path.insert(0, '/opt/fixloop/runtime')
        p = types.ModuleType('agent_runtime')
        p.__path__ = ['/opt/fixloop/runtime/agent_runtime']
        sys.modules['agent_runtime'] = p
        from agent_runtime.linux_sandbox.workspace_controller import worker_argv
        from agent_runtime.linux_sandbox.guarded_fs import private_binfmt
        socket.create_connection(('172.25.32.1', 7897), timeout=3).close()
        with tempfile.TemporaryDirectory(prefix='fixloop-s3-isolation-') as directory:
          with private_binfmt():
            argv = worker_argv(Path(directory))
            result = subprocess.run(argv[:-1] + [sys.argv[1]], capture_output=True,
                                    text=True, timeout=15, close_fds=True)
            if result.returncode:
                raise RuntimeError(result.stderr)
            print(result.stdout)
    """)
    # This probe owns the exact diagnostic program; retain stderr on failure.
    isolation_process = subprocess.run(
        [
            str(Path(os.environ["SystemRoot"]) / "System32/wsl.exe"),
            "--distribution",
            prepared.identity.distribution,
            "--user",
            "fixloop",
            "--exec",
            "/usr/bin/unshare",
            "--user",
            "--map-root-user",
            "--mount",
            "/usr/bin/python3.14",
            "-I",
            "-B",
            "-c",
            isolation_controller,
            isolation_worker,
        ],
        capture_output=True,
        timeout=30,
    )
    if isolation_process.returncode:
        raise RuntimeError(decode_wsl(isolation_process.stderr))
    isolated = json.loads(isolation_process.stdout)
    report["worker_isolation"] = isolated
    for name, passed in isolated["checks"].items():
        check("worker_" + name, passed)

    def bind(root, task):
        actual_repository = root == workspace
        spec = SandboxTaskSpec(
            prepared.identity,
            "s3-v1",
            hashlib.sha256(b"s3-v1").hexdigest(),
            "" if actual_repository else str(fixture / "state"),
        )
        binding = SandboxTaskBinding.create(
            root if actual_repository else repository, root, task, spec
        )
        backend = GuardedWindowsWorkspaceBackend(binding)

        def forbidden(*_a, **_kw):
            raise AssertionError("host source resolver used")

        context = ToolContext(str(root), path_resolver=forbidden, sandbox_backend=backend)
        binding.attach(context)
        return context

    def call(context, name, arguments):
        started = time.monotonic()
        # Preserve controller stderr only for explicit local acceptance diagnosis.
        # Product transport and the verified installed runtime remain unchanged.
        diagnostic = fixture / ("controller-" + str(len(calls)) + ".stderr")
        native_popen = subprocess.Popen

        def diagnostic_popen(argv, **kwargs):
            if any(str(value).endswith("/workspace_controller.py") for value in argv):
                kwargs["stderr"] = error_stream
            return native_popen(argv, **kwargs)

        with diagnostic.open("wb") as error_stream:
            with mock_patch("subprocess.Popen", diagnostic_popen):
                result = execute_workspace_tool(context, name, arguments)
        if not result.ok:
            print(diagnostic.read_text(encoding="utf-8", errors="replace"), flush=True)
            print("tool_error: " + result.content, flush=True)
        calls.append(
            {
                "tool": name,
                "arguments": arguments,
                "status": str(result.status),
                "error_code": result.error_code,
                "receipt": result.receipt,
                "changed_files": result.changed_files,
                "metadata": result.metadata,
                "wall_ms": round((time.monotonic() - started) * 1000),
            }
        )
        return result

    actual = bind(workspace, "s3-actual-" + uuid.uuid4().hex[:8])
    check("actual_repository_read", call(actual, "read_file", {"path": "CLAUDE.md"}).ok)
    for root, label in ((repository, "repository"), (worktree, "worktree")):
        context = bind(root, "s3-" + label)
        (root / ".env").write_text("S3-sensitive-sentinel")
        (root / "sitecustomize.py").write_text("raise RuntimeError('must never be imported')\n")
        outside = fixture / (label + "-outside.txt")
        outside.write_text("S3-outside-sentinel")
        os.link(outside, root / "alias.txt")
        outside_directory = fixture / (label + "-outside-dir")
        outside_directory.mkdir()
        (outside_directory / "secret.txt").write_text("S3-junction-sentinel")
        # No shell interpolation: native PowerShell receives separately quoted,
        # generated fixture paths and creates an explicit Windows reparse point.
        junction = root / "junction"
        subprocess.run(
            [
                "powershell.exe",
                "-NoProfile",
                "-Command",
                "New-Item -ItemType Junction -Path '"
                + str(junction).replace("'", "''")
                + "' -Target '"
                + str(outside_directory).replace("'", "''")
                + "' | Out-Null",
            ],
            check=True,
            capture_output=True,
        )
        check(label + "_read", call(context, "read_file", {"path": "src/add.py"}).ok)
        for name, arguments in (
            ("list_files", {"path": "."}),
            ("grep", {"path": ".", "pattern": "return"}),
            ("ast_parse", {"path": "src/add.py"}),
            ("inspect_file", {"path": "src/add.py"}),
            ("find_test", {"file_path": "src/add.py", "function_name": "add"}),
        ):
            result = call(context, name, arguments)
            check(label + "_" + name, result.ok)
            check(label + "_" + name + "_no_secret", "sentinel" not in result.content)
        for raw in (
            ".env",
            ".git",
            "../" + outside.name,
            "alias.txt",
            ".aws/credentials",
            "junction/secret.txt",
        ):
            check(label + "_denied_" + raw, not call(context, "read_file", {"path": raw}).ok)
        result = call(
            context, "patch_file", {"path": "src/add.py", "old_text": "a + b", "new_text": "a - b"}
        )
        check(
            label + "_patch_committed",
            result.ok
            and result.changed_files == ["src/add.py"]
            and "a - b" in (root / "src/add.py").read_text(),
        )
        check(label + "_reread", call(context, "read_file", {"path": "src/add.py"}).ok)
        (root / "src/add.py").write_text("def user_edit():\n    pass\n")
        result = call(context, "write_file", {"path": "src/add.py", "content": "pass\n"})
        check(
            label + "_stale_write_preserves_user",
            not result.ok and (root / "src/add.py").read_text() == "def user_edit():\n    pass\n",
        )
        result = call(context, "write_file", {"path": "new.txt", "content": "new"})
        check(label + "_conditional_create", result.ok and (root / "new.txt").read_text() == "new")
        patch = "*** Begin Patch\n*** Add File: added.txt\n+added\n*** End Patch"
        result = call(context, "apply_patch", {"patch": patch})
        check(label + "_apply_patch_committed", result.ok and result.changed_files == ["added.txt"])
        receipt = context.sandbox_backend.inspect_receipt(result.receipt["id"])
        check(
            label + "_durable_commit_receipt",
            receipt["state"] == "terminal"
            and receipt["result"]["mutation_status"] == "committed"
            and receipt["result"]["cleanup"] == "confirmed",
        )
        check(
            label + "_unmigrated_rejected",
            call(context, "run_shell", {"command": "echo should-not-run"}).error_code
            == "policy_denied",
        )
        check(label + "_outside_unchanged", outside.read_text() == "S3-outside-sentinel")
        check(
            label + "_receipts_confirmed",
            all(
                item["metadata"].get("sandbox_cleanup") == "confirmed"
                for item in calls
                if item["receipt"].get("id")
            ),
        )

    # Cancel after a real FUSE RPC begins. Hold that one broker response briefly
    # so the worker remains live; this changes no product runtime or file bytes.
    import agent_runtime.linux_sandbox.workspace_backend as module

    original_broker = module.GuardedWorkspaceBroker
    entered, release = threading.Event(), threading.Event()

    class DelayedBroker(original_broker):
        def call(self, request):
            value = super().call(request)
            if not entered.is_set():
                entered.set()
                release.wait(3)
            return value

    context = bind(repository, "s3-cancel")
    results = []
    module.GuardedWorkspaceBroker = DelayedBroker
    thread = threading.Thread(
        target=lambda: results.append(call(context, "read_file", {"path": "README.md"}))
    )
    try:
        thread.start()
        check("cancel_worker_entered_fuse", entered.wait(45))
        active = list(context.sandbox_backend._active)
        check("cancel_requested", len(active) == 1 and context.sandbox_backend.cancel(active[0]))
        release.set()
        thread.join(15)
        check("cancel_bounded", not thread.is_alive())
        check(
            "cancel_confirmed",
            len(results) == 1
            and results[0].status == "cancelled"
            and results[0].metadata["sandbox_cleanup"] == "confirmed",
        )
    finally:
        release.set()
        thread.join(15)
        module.GuardedWorkspaceBroker = original_broker

    cleanup_code = (
        "import glob,json,os; "
        "names=glob.glob('/tmp/fixloop-s0-view-*')+glob.glob('/tmp/fixloop-s0-binfmt-*'); "
        "live=[]\n"
        "for path in glob.glob('/proc/[0-9]*/cmdline'):\n"
        " try:\n"
        "  if path.split('/')[2]==str(os.getpid()): continue\n"
        "  argv=open(path,'rb').read().split(b'\\0')\n"
        "  if any(a.endswith(b'/workspace_controller.py') for a in argv): live.append(path)\n"
        " except OSError: pass\n"
        "print(json.dumps({'temporary_mounts':names,'controllers':live}))"
    )
    cleanup = json.loads(
        SandboxEnvironmentManager(workspace).transport.execute(
            prepared.identity.distribution,
            ["/usr/bin/python3.14", "-I", "-B", "-c", cleanup_code],
            user="fixloop",
        )
    )
    report["cleanup"] = cleanup
    check(
        "no_controller_or_fuse_leak", not cleanup["temporary_mounts"] and not cleanup["controllers"]
    )
    report["status"] = "passed"
    args.report.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
