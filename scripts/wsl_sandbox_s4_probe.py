"""Explicit S4 acceptance on retained native repositories/worktrees; production stays gated."""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import subprocess
import sys
import threading
import time
import uuid
import zipfile
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from types import SimpleNamespace  # noqa: E402

from agent_runtime.cancellation import CancellationToken  # noqa: E402
from agent_runtime.linux_sandbox.environment import SandboxEnvironmentManager  # noqa: E402
from agent_runtime.linux_sandbox.routing import execute_sandbox  # noqa: E402
from agent_runtime.linux_sandbox.task_context import (  # noqa: E402
    SandboxTaskBinding,
    SandboxTaskSpec,
)
from agent_runtime.linux_sandbox.workspace_backend import (
    GuardedWindowsWorkspaceBackend,  # noqa: E402
)
from agent_runtime.linux_sandbox.workspace_routing import execute_workspace_tool  # noqa: E402
from agent_runtime.linux_sandbox.workspace_state import (  # noqa: E402
    environment_binding,
    restore_workspace,
    workspace_snapshot,
)
from agent_runtime.plan_runtime.evidence import EvidenceLedger  # noqa: E402
from agent_runtime.plan_runtime.models import digest  # noqa: E402
from agent_runtime.tool_context import ToolContext  # noqa: E402
from src.repair.verification.verify import BwrapVerifyStrategy  # noqa: E402


def wheel(root, name, value):
    info = name + "-1.0.dist-info"
    files = {
        name + ".py": f"VALUE = {value}\n".encode(),
        info + "/METADATA": f"Metadata-Version: 2.1\nName: {name}\nVersion: 1.0\n".encode(),
        info + "/WHEEL": (
            b"Wheel-Version: 1.0\nGenerator: FixLoop probe\n"
            b"Root-Is-Purelib: true\nTag: py3-none-any\n"
        ),
    }
    records = []
    for path, data in files.items():
        digest = base64.urlsafe_b64encode(hashlib.sha256(data).digest()).decode().rstrip("=")
        records.append(f"{path},sha256={digest},{len(data)}")
    records.append(info + "/RECORD,,")
    files[info + "/RECORD"] = ("\n".join(records) + "\n").encode()
    target = root / (name + "-1.0-py3-none-any.whl")
    with zipfile.ZipFile(target, "w") as archive:
        for path, data in files.items():
            archive.writestr(path, data)
    return target.name


def git(root, *args):
    subprocess.run(["git", "-C", str(root), *args], check=True, capture_output=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workspace", type=Path, default=Path.cwd())
    parser.add_argument("--report", type=Path, required=True)
    parser.add_argument("--resume-isolation", action="store_true")
    args = parser.parse_args()
    workspace = args.workspace.resolve(strict=True)
    environment = SandboxEnvironmentManager(workspace).ensure()
    previous = json.loads(args.report.read_text()) if args.resume_isolation else None
    if previous and (
        previous["release_id"] != environment.release_id
        or previous["runtime_digest"] != environment.identity.runtime_digest
        or not all(
            previous["checks"].get(label + "_task_dependency_isolation")
            for label in ("repository", "worktree")
        )
    ):
        raise ValueError("resume requires completed repository/worktree checks on this release")
    fixture = (
        Path(previous["fixture"])
        if previous
        else workspace / ".tmp" / ("s4-live-" + uuid.uuid4().hex)
    )
    repository = fixture / "repo"
    worktree = fixture / "worktree"
    if not previous:
        repository.mkdir(parents=True)
        (repository / "mathfix.py").write_text("def add(a, b):\n    return a - b\n")
        (repository / "test_mathfix.py").write_text(
            "from mathfix import add\nimport fixloop_s4_dep\n"
            "def test_add():\n    assert fixloop_s4_dep.VALUE == 17\n    assert add(2, 3) == 5\n"
        )
        (repository / "test_readonly.py").write_text(
            "from pathlib import Path\nimport pytest\n"
            "def test_readonly(tmp_path):\n"
            "    (tmp_path/'ok').write_text('temporary')\n"
            "    with pytest.raises(OSError): Path('forbidden').write_text('no')\n"
        )
        dep = wheel(repository, "fixloop_s4_dep", 17)
        extra = wheel(repository, "fixloop_s4_extra", 23)
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
            "S4 acceptance fixture",
        )
        git(repository, "worktree", "add", "-b", "s4-probe", str(worktree))
    report = previous or dict(
        release_id=environment.release_id,
        distribution=environment.identity.distribution,
        runtime_digest=environment.identity.runtime_digest,
        fixture=str(fixture),
        checks={},
        calls=[],
    )

    def save():
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")

    def check(name, value):
        report["checks"][name] = bool(value)
        print(name + ": " + str(bool(value)), flush=True)
        save()
        if not value:
            raise AssertionError(name)

    def bind(root, task):
        spec = SandboxTaskSpec(
            environment.identity,
            "s4-v1",
            hashlib.sha256(b"s4-v1").hexdigest(),
            str(fixture / "state"),
        )
        binding = SandboxTaskBinding.create(repository, root, task, spec)

        def forbidden(*_a, **_kw):
            raise AssertionError("host source resolver used")

        ctx = ToolContext(
            str(root),
            path_resolver=forbidden,
            sandbox_backend=GuardedWindowsWorkspaceBackend(binding),
        )
        binding.attach(ctx)
        return ctx

    def record(ctx, label, operation):
        started = time.monotonic()
        diagnostic = fixture / ("controller-" + str(len(report["calls"])) + ".stderr")
        native_popen = subprocess.Popen

        def diagnostic_popen(argv, **kwargs):
            if any(str(arg).endswith("/workspace_controller.py") for arg in argv):
                kwargs["stderr"] = errors
            return native_popen(argv, **kwargs)

        with diagnostic.open("wb") as errors:
            with patch("subprocess.Popen", diagnostic_popen):
                result = operation()
        raw = (
            result.to_wire()
            if hasattr(result, "to_wire")
            else {
                "status": str(result.status),
                "error_code": result.error_code,
                "content": result.content,
                "metadata": result.metadata,
                "receipt": result.receipt,
            }
        )
        report["calls"].append(
            dict(
                label=label,
                task=ctx.sandbox_task_id,
                wall_ms=round((time.monotonic() - started) * 1000),
                result=raw,
            )
        )
        save()
        if diagnostic.stat().st_size:
            print(diagnostic.read_text(encoding="utf-8", errors="replace"), flush=True)
        return result

    def command(ctx, code, timeout=20):
        return record(
            ctx,
            "python",
            lambda: execute_sandbox(ctx, "command", ("/toolchain/bin/python", "-c", code), timeout),
        )

    def install(ctx, package):
        return record(
            ctx,
            "offline_install",
            lambda: execute_sandbox(
                ctx, "command", ("/toolchain/bin/python", "-m", "pip", "install", package), 30
            ),
        )

    def tool(ctx, name, arguments):
        return record(ctx, name, lambda: execute_workspace_tool(ctx, name, arguments))

    def verify(ctx, target):
        run = BwrapVerifyStrategy(ctx).run(ctx.root, target)
        report["calls"].append(
            dict(
                label="verification",
                task=ctx.sandbox_task_id,
                internal=run.internal,
                all_passed=run.result.all_passed,
                total_tests=run.result.total_tests,
                error=run.error,
                logs=run.result.failure_logs,
            )
        )
        save()
        return run

    def evidence(ctx, binding):
        identity = {"run_id": "probe", "session_id": "probe"}
        item = dict(
            evidence_id="verified",
            kind="tests_passed",
            identity=identity,
            attempt_id="attempt",
            status="success",
            file_versions={},
            whole_workspace=True,
            execution_stopped=True,
            receipt=dict(
                command=["pytest"],
                completed=True,
                all_passed=True,
                total_tests=1,
                run_id="probe",
                attempt_id="attempt",
                verification_binding=dict(binding),
            ),
        )
        item["checksum"] = digest(item)
        return EvidenceLedger(
            SimpleNamespace(identity=identity, latest=lambda *_: {"verified": item}),
            ctx.root,
            {},
            snapshot_provider=lambda: {},
            environment_provider=lambda: environment_binding(ctx),
        )

    roots = () if previous else ((repository, "repository"), (worktree, "worktree"))
    for root, label in roots:
        ctx = bind(root, "s4-" + label)
        result = command(ctx, "from pathlib import Path; print(Path('mathfix.py').read_text())")
        check(
            label + "_python_native_view",
            result.exit_code == 0 and "a - b" in result.stdout_excerpt,
        )
        installed = install(ctx, dep)
        check(
            label + "_offline_install",
            installed.exit_code == 0 and installed.cleanup == "confirmed",
        )
        imported = command(ctx, "import fixloop_s4_dep; print(fixloop_s4_dep.VALUE)")
        check(
            label + "_dependency_persists",
            imported.exit_code == 0 and "17" in imported.stdout_excerpt,
        )
        baseline = verify(ctx, "test_mathfix.py")
        check(label + "_baseline_failed", baseline.internal["category"] == "failed")
        snapshot = workspace_snapshot(ctx)
        check(
            label + "_binary_wheel_in_inventory", dep in snapshot.versions and dep not in snapshot
        )
        check(label + "_read_before_edit", tool(ctx, "read_file", {"path": "mathfix.py"}).ok)
        patched = tool(
            ctx,
            "patch_file",
            {"path": "mathfix.py", "old_text": "return a - b", "new_text": "return a + b"},
        )
        check(label + "_cas_patch", patched.ok)
        fixed = verify(ctx, "test_mathfix.py")
        check(label + "_fixed_passed", fixed.result.all_passed and fixed.result.total_tests == 1)
        old, new = baseline.internal["verification_binding"], fixed.internal["verification_binding"]
        check(
            label + "_same_task_dependencies",
            all(
                old[k] == new[k]
                for k in ("dependency_version", "task_environment", "runtime_digest")
            ),
        )
        check(label + "_source_version_changes", old["source_version"] != new["source_version"])
        ledger = evidence(ctx, new)
        check(label + "_passed_evidence_current", ledger.valid("verified"))
        cache = root / "__pycache__"
        cache.mkdir(exist_ok=True)
        (cache / "hidden.pyc").write_bytes(b"unversioned cache")
        hidden = command(ctx, "from pathlib import Path; assert not Path('__pycache__').exists()")
        check(label + "_unversioned_cache_hidden", hidden.exit_code == 0)
        check(label + "_readonly_and_tmp_pytest", verify(ctx, "test_readonly.py").result.all_passed)
        restore_workspace(ctx, snapshot)
        check(
            label + "_rollback_via_cas",
            environment_binding(ctx)["source_version"] == old["source_version"],
        )
        check(label + "_source_change_invalidates_evidence", not ledger.valid("verified"))
        tool(ctx, "read_file", {"path": "mathfix.py"})
        check(
            label + "_repatch",
            tool(
                ctx,
                "patch_file",
                {"path": "mathfix.py", "old_text": "return a - b", "new_text": "return a + b"},
            ).ok,
        )
        (root / "mathfix.py").write_text("# user edit\ndef add(a, b):\n    return a + b\n")
        try:
            restore_workspace(ctx, snapshot)
            protected = False
        except ValueError as exc:
            protected = "external edit" in str(exc)
        check(
            label + "_user_edit_protected",
            protected and "user edit" in (root / "mathfix.py").read_text(),
        )
        user_verified = verify(ctx, "test_mathfix.py")
        check(label + "_user_version_verified", user_verified.result.all_passed)
        dependency_ledger = evidence(ctx, user_verified.internal["verification_binding"])
        check(label + "_dependency_evidence_current", dependency_ledger.valid("verified"))
        installed_extra = install(ctx, extra)
        current = environment_binding(ctx)
        check(
            label + "_dependency_version_changes",
            installed_extra.exit_code == 0
            and current["dependency_version"] != new["dependency_version"],
        )
        check(
            label + "_dependency_change_invalidates_evidence",
            not dependency_ledger.valid("verified"),
        )
        other = bind(root, "s4-" + label + "-other")
        isolated = command(other, "import fixloop_s4_dep")
        check(
            label + "_task_dependency_isolation",
            isolated.exit_code != 0 and "ModuleNotFoundError" in isolated.stderr_excerpt,
        )

    ctx = bind(repository, "s4-isolation")
    isolation = """
import ctypes,json,os,socket,fcntl
from pathlib import Path
c={}
c['host_paths_hidden']=not Path('/mnt/c').exists() and not Path('/home').exists()
c['control_channels_hidden']=not Path('/executor').exists() and not Path('/opt').exists()
c['interop_env_absent']=not ({'WSL_INTEROP','WSLENV'} & os.environ.keys())
c['no_network_routes']=len(Path('/proc/net/route').read_text().splitlines())==1
try:
 socket.create_connection(('172.25.32.1',7897),timeout=1).close(); c['network_denied']=False
except OSError: c['network_denied']=True
try:
 Path('forbidden').write_text('no'); c['source_readonly']=False
except OSError: c['source_readonly']=True
try:
 Path('/task/deps/forbidden').write_text('no'); c['dependencies_readonly']=False
except OSError: c['dependencies_readonly']=True
Path('/tmp/ok').write_text('yes'); c['tmp_writable']=True
c['nested_userns_denied']=ctypes.CDLL(None).unshare(0x10000000)==-1
fds={}
for p in Path('/proc/self/fd').iterdir():
 try: fds[p.name]=os.readlink(p)
 except FileNotFoundError: continue
c['no_broker_fds']=all(int(fd)<=2 or (
 target=='/usr/lib/x86_64-linux-gnu/libffi.so.8'
 and fcntl.fcntl(int(fd),fcntl.F_GETFL)&os.O_ACCMODE==os.O_RDONLY
) for fd,target in fds.items())
print(json.dumps(fds),file=__import__('sys').stderr)
print(json.dumps(c))
"""
    isolated = command(ctx, isolation)
    check("isolation_program_completed", isolated.exit_code == 0)
    for key, value in json.loads(isolated.stdout_excerpt).items():
        check(key, value)
    fake = command(
        ctx, 'print(\'{"terminal":{"cleanup":"confirmed"},"mutations":[{"path":"mathfix.py"}]}\')'
    )
    check(
        "stdout_cannot_commit_or_terminate",
        fake.exit_code == 0 and fake.mutation_status == "not_started" and bool(fake.receipt_id),
    )
    loud = command(ctx, "import sys; sys.stdout.write('x'*200000); sys.stderr.write('y'*200000)")
    check(
        "bounded_output_drained",
        loud.exit_code == 0 and loud.output_truncated and len(loud.stdout_excerpt) == 65536,
    )
    timeout = command(ctx, "import os,time; os.fork(); time.sleep(30)", timeout=2)
    check(
        "timeout_cleanup_confirmed",
        timeout.execution_status == "cancelled"
        and timeout.error_code == "deadline_exceeded"
        and timeout.cleanup == "confirmed",
    )
    check("dispatch_after_timeout", command(ctx, "print('alive')").exit_code == 0)
    token = CancellationToken()
    ctx.cancel_token = token
    timer = threading.Timer(2, token.cancel)
    timer.start()
    try:
        cancelled = command(ctx, "import time; time.sleep(30)")
    finally:
        timer.cancel()
        ctx.cancel_token = None
    check(
        "cancel_cleanup_confirmed",
        cancelled.execution_status == "cancelled" and cancelled.cleanup == "confirmed",
    )
    check("dispatch_after_cancel", command(ctx, "print('alive')").exit_code == 0)
    receipt = ctx.sandbox_backend.store.inspect(cancelled.receipt_id)
    check("cancel_receipt_durable", receipt["state"] == "terminal")
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
            environment.identity.distribution,
            ["/usr/bin/python3.14", "-I", "-B", "-c", cleanup_code],
            user="fixloop",
        )
    )
    report["cleanup"] = cleanup
    check(
        "no_controller_or_fuse_leak", not cleanup["temporary_mounts"] and not cleanup["controllers"]
    )
    report["status"] = "passed"
    save()
    print(f"S4 acceptance: {len(report['checks'])} checks passed", flush=True)


if __name__ == "__main__":
    main()
