"""S0 Windows-workspace feasibility probe, NOT an execution backend.

Creates only disposable fixtures under --workspace-parent. No project code is
executed and no WSL settings, distributions or packages are changed. A successful
process launch is not a security pass: failed or missing checks produce no_go.
"""

from __future__ import annotations

import argparse
import contextlib
import importlib.util
import json
import os
import platform
import shutil
import socket
import stat
import subprocess
import sys
import tempfile
import threading
from pathlib import Path

SCHEMA = "fixloop-sandbox-s0-v1"
MARKER = "fixloop-disposable-secret"
INTEROP_MARKER = "FIXLOOP_S0_INTEROP_EXECUTED"
LIBRARIES = (
    "libbz2.so.1.0",
    "libc.so.6",
    "libcrypto.so.3",
    "libdb-5.3.so",
    "libexpat.so.1",
    "libffi.so.8",
    "libgdbm.so.6",
    "liblzma.so.5",
    "libm.so.6",
    "libncursesw.so.6",
    "libpanelw.so.6",
    "libreadline.so.8",
    "libsqlite3.so.0",
    "libssl.so.3",
    "libtinfo.so.6",
    "libuuid.so.1",
    "libz.so.1",
    "libzstd.so.1",
)
TARGET_CHECKS = frozenset(
    {
        "workspace_read",
        "workspace_write",
        "unicode_and_spaces",
        "env_mask",
        "git_mask",
        "git_entry_mask",
        "control_mask",
        "env_case_alias",
        "git_case_alias",
        "control_case_alias",
        "secret_hardlink",
        "external_hardlink",
        "junction_escape",
        "external_symlink",
        "relative_escape",
        "proc_root_escape",
        "host_paths_hidden",
        "private_pid",
        "environment_clean",
        "no_inherited_fds",
        "network_isolated",
        "windows_execution_blocked",
        "toolchain_read_only",
        "tmpfs_capacity",
    }
)
GUARDED_CHECKS = frozenset(
    {
        "workspace_listing",
        "workspace_rename",
        "workspace_delete",
        "workspace_mkdir",
        "link_creation_denied",
        "symlink_creation_denied",
        "protected_rename_denied",
        "ads_denied",
        "chmod_denied",
        "userns_creation_blocked",
    }
)


def run(
    argv: list[str], *, timeout: int = 20, env: dict | None = None, input_text: str | None = None
) -> subprocess.CompletedProcess:
    return subprocess.run(
        argv,
        input=input_text,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
        check=False,
        env=env,
    )


def wsl_path(path: str | Path) -> str:
    """Translate only local absolute drive paths; never interpolate shell text."""
    from pathlib import PureWindowsPath

    candidate = PureWindowsPath(path)
    if (
        not candidate.is_absolute()
        or len(candidate.drive) != 2
        or candidate.drive[1] != ":"
        or not candidate.drive[0].isascii()
        or not candidate.drive[0].isalpha()
        or any(part in ("..", ".") for part in candidate.parts)
        or "\x00" in str(path)
    ):
        raise ValueError("local absolute Windows drive path required")
    return "/mnt/" + candidate.drive[0].lower() + "/" + "/".join(candidate.parts[1:])


def outcome(checks: dict) -> dict:
    blockers = [name for name, result in checks.items() if result.get("passed") is not True]
    return {
        "checks": checks,
        "blockers": blockers,
        "probe_passed": bool(checks) and not blockers,
        "decision": "go_for_next_stage" if checks and not blockers else "no_go",
    }


def target_evidence(stdout: str, *, guarded=False) -> dict:
    observed = json.loads(stdout)
    if (
        not isinstance(observed, dict)
        or set(observed) != TARGET_CHECKS | (GUARDED_CHECKS if guarded else set())
        or any(
            not isinstance(value, dict) or type(value.get("passed")) is not bool
            for value in observed.values()
        )
    ):
        raise ValueError("missing or invalid target evidence")
    return observed


def mask_arguments(workspace: Path) -> list[str]:
    """Experimental masks, deliberately exercised against aliases by this probe."""
    args = []
    for name in (".git", ".agent", ".env"):
        source = workspace / name
        target = "/workspace/" + name
        if source.is_dir():
            args += ["--tmpfs", target, "--remount-ro", target]
        else:
            args += ["--ro-bind", "/dev/null", target]
    return args


def sandbox_arguments(
    workspace: Path, toolchain: Path, python: Path, *, guarded=False
) -> list[str]:
    """Narrow Python 3.14/x86-64 diagnostic closure; no broad /usr or drive bind."""
    lib = Path("/usr/lib/x86_64-linux-gnu")
    args = [
        "/usr/bin/bwrap",
        "--unshare-user",
        *(["--disable-userns"] if guarded else []),
        "--unshare-pid",
        "--unshare-net",
        "--unshare-ipc",
        "--unshare-uts",
        "--die-with-parent",
        "--new-session",
        "--clearenv",
        "--cap-drop",
        "ALL",
        "--dir",
        "/usr",
        "--dir",
        "/usr/bin",
        "--dir",
        "/usr/lib",
        "--dir",
        str(lib),
        "--dir",
        "/lib64",
        "--ro-bind",
        str(python),
        str(python),
        "--symlink",
        "python3.14",
        "/usr/bin/python3",
        "--ro-bind",
        "/usr/lib/python3.14",
        "/usr/lib/python3.14",
        "--ro-bind",
        str(lib / "ld-linux-x86-64.so.2"),
        "/lib64/ld-linux-x86-64.so.2",
    ]
    for name in LIBRARIES:
        args += ["--ro-bind", str(lib / name), str(lib / name)]
    args += [
        "--ro-bind",
        str(toolchain),
        "/toolchain",
        "--bind",
        str(workspace),
        "/workspace",
        *([] if guarded else mask_arguments(workspace)),
    ]
    # A real PE binary tests binfmt dispatch even when /mnt/c is absent. It is
    # bound individually read-only; an outside echo provides a positive control.
    args += [
        "--ro-bind",
        "/mnt/c/Windows/System32/cmd.exe",
        "/interop-probe.exe",
        "--proc",
        "/proc",
        "--dev",
        "/dev",
        "--size",
        "1048576",
        "--tmpfs",
        "/tmp",
        "--dir",
        "/home",
        "--symlink",
        "/tmp",
        "/home/sandbox",
        "--setenv",
        "PATH",
        "/toolchain/bin",
        "--setenv",
        "HOME",
        "/home/sandbox",
        "--setenv",
        "TMPDIR",
        "/tmp",
        "--setenv",
        "LANG",
        "C.UTF-8",
        "--setenv",
        "PYTEST_DISABLE_PLUGIN_AUTOLOAD",
        "1",
        "--chdir",
        "/workspace",
    ]
    return args


TARGET = r"""
import errno, json, os, pathlib, socket, subprocess, sys
p = pathlib.Path
checks = {}
def check(name, passed, **detail):
    checks[name] = dict(passed=bool(passed), **detail)
def access(relative):
    path = p('/workspace') / relative
    readable, writable = False, False
    try:
        readable = bool(path.read_bytes())
    except (OSError, UnicodeError):
        pass
    try:
        path.write_text('fixloop-disposable-secret|tampered')
        writable = True
    except OSError:
        pass
    return dict(readable=readable, writable=writable)
source = p('/workspace/source.py')
check('workspace_read', source.read_text() == 'value = 1\n')
p('/workspace/result.txt').write_text('persistent')
check('workspace_write', p('/workspace/result.txt').read_text() == 'persistent')
check('unicode_and_spaces', p('/workspace/中文 文件.txt').read_text() == 'unicode')
for name, relative in {
    'env_mask': '.env', 'git_mask': '.git/config', 'git_entry_mask': '.git',
    'control_mask': '.agent/state',
    'env_case_alias': '.ENV', 'git_case_alias': '.GIT/config',
    'control_case_alias': '.AGENT/state', 'secret_hardlink': 'secret-alias',
    'external_hardlink': 'outside-alias', 'junction_escape': 'escape-junction/sentinel',
    'external_symlink': 'escape-symlink', 'relative_escape': '../external/sentinel',
    'proc_root_escape': '/proc/1/root/mnt/c/Windows/System32/cmd.exe',
}.items():
    if relative.startswith('/'):
        check(name, not p(relative).exists())
    else:
        detail = access(relative)
        check(name, not any(detail.values()), **detail)
hidden = ['/mnt', '/run/WSL', '/init', '/proc/sys/fs/binfmt_misc/WSLInterop', sys.argv[2]]
check('host_paths_hidden', not any(p(x).exists() for x in hidden))
check('private_pid', os.getpid() == 2, pid=os.getpid())
blocked_env = ['WSL_INTEROP', 'WSLENV', 'FIXLOOP_S0_FAKE_TOKEN', 'HTTP_PROXY']
check('environment_clean', not any(x in os.environ for x in blocked_env))
fds = []
for fd in p('/proc/self/fd').iterdir():
    try:
        number = int(fd.name)
        if number > 2:
            fds.append(os.readlink(fd))
    except OSError:
        pass
check('no_inherited_fds', not fds)
try:
    with socket.socket() as sock:
        sock.settimeout(2)
        connected = sock.connect_ex(('127.0.0.1', int(sys.argv[1]))) == 0
    check('network_isolated', not connected)
except OSError:
    check('network_isolated', True)
try:
    result = subprocess.run(['/interop-probe.exe', '/d', '/c', 'echo',
                             'FIXLOOP_S0_INTEROP_EXECUTED'],
                            capture_output=True, timeout=3)
    # A running binfmt handler does not prove a Windows child started. Keep it
    # inconclusive unless an OS-level denial is observed, and retain evidence.
    confirmed = b'FIXLOOP_S0_INTEROP_EXECUTED' in result.stdout
    check('windows_execution_blocked', False, exit_code=result.returncode,
          windows_execution_confirmed=confirmed, handler_started=True,
          stdout=result.stdout.decode('utf-8', errors='replace')[-500:],
          stderr=result.stderr.decode('utf-8', errors='replace')[-1000:])
except OSError as exc:
    check('windows_execution_blocked', exc.errno in (errno.ENOENT, errno.ENOEXEC, errno.EACCES),
          errno=exc.errno)
except subprocess.TimeoutExpired:
    check('windows_execution_blocked', False, timed_out=True)
try:
    p('/toolchain/pyvenv.cfg').write_text('tampered')
    check('toolchain_read_only', False)
except OSError as exc:
    check('toolchain_read_only', exc.errno == errno.EROFS, errno=exc.errno)
check('tmpfs_capacity', os.statvfs('/tmp').f_blocks * os.statvfs('/tmp').f_frsize == 1048576)
if sys.argv[3] == 'guarded':
    entries = os.listdir('/workspace')
    check('workspace_listing', 'source.py' in entries and
          not {'.env', '.git', '.agent', 'secret-alias', 'outside-alias'} & set(entries))
    first, second = p('/workspace/rename-source'), p('/workspace/renamed-file')
    first.write_text('rename-data')
    os.rename(first, second)
    check('workspace_rename', not first.exists() and second.read_text() == 'rename-data')
    def denied(name, operation):
        try:
            operation()
            check(name, False)
        except OSError as exc:
            check(name, exc.errno in (errno.EPERM, errno.EACCES), errno=exc.errno)
    denied('link_creation_denied', lambda: os.link(second, '/workspace/new-link'))
    denied('symlink_creation_denied', lambda: os.symlink('/tmp', '/workspace/new-symlink'))
    denied('protected_rename_denied', lambda: os.rename(second, '/workspace/.ENV'))
    denied('ads_denied', lambda: p('/workspace/source.py:secret').write_text('ads'))
    denied('chmod_denied', lambda: second.chmod(0o777))
    second.unlink()
    check('workspace_delete', not second.exists())
    directory = p('/workspace/new-directory')
    directory.mkdir()
    (directory / 'file').write_text('nested')
    nested_ok = (directory / 'file').read_text() == 'nested'
    (directory / 'file').unlink()
    directory.rmdir()
    check('workspace_mkdir', nested_ok and not directory.exists())
    import ctypes
    libc = ctypes.CDLL(None, use_errno=True)
    value = libc.unshare(0x10000000)
    check('userns_creation_blocked', value == -1 and ctypes.get_errno() in
          (errno.EPERM, errno.EACCES, errno.ENOSPC), errno=ctypes.get_errno())
print(json.dumps(checks))
"""


def linux_probe(args: argparse.Namespace) -> dict:
    root, toolchain = Path(args.fixture_root), Path(args.toolchain)
    python = Path("/usr/bin/python3.14")
    checks: dict = {}
    environment = {"kernel": os.uname().release, "architecture": platform.machine()}
    required = [
        Path("/usr/bin/bwrap"),
        python,
        Path("/usr/lib/python3.14"),
        toolchain / "bin/python",
        toolchain / "pyvenv.cfg",
        Path("/mnt/c/Windows/System32/cmd.exe"),
    ]
    supported = (
        "microsoft" in os.uname().release.casefold()
        and platform.machine() == "x86_64"
        and all(path.exists() for path in required)
        and str(root).startswith("/mnt/")
        and root.name.startswith(".fixloop-s0-")
        and root.is_dir()
        and not toolchain.resolve().is_relative_to(root.resolve())
    )
    checks["supported_environment"] = {"passed": supported}
    if not supported:
        return {"environment": environment, **outcome(checks)}
    filesystem = run(["/usr/bin/stat", "-f", "-c", "%T", str(root)])
    environment["workspace_filesystem"] = filesystem.stdout.strip()
    environment["bwrap"] = run(["/usr/bin/bwrap", "--version"]).stdout.strip()
    environment["python"] = run([str(python), "--version"]).stdout.strip()
    environment["interop_registered_outside"] = Path("/proc/sys/fs/binfmt_misc/WSLInterop").exists()
    control = run(["/mnt/c/Windows/System32/cmd.exe", "/d", "/c", "echo", INTEROP_MARKER])
    checks["interop_control"] = {
        "passed": control.returncode == 0 and INTEROP_MARKER in control.stdout,
        "exit_code": control.returncode,
        "stderr": control.stderr[-500:],
    }
    checks["windows_mount_verified"] = {
        "passed": filesystem.returncode == 0 and filesystem.stdout.strip() in ("v9fs", "9p"),
    }
    # Capability presence matters more than the exact distro marketing version.
    help_result = run(["/usr/bin/bwrap", "--help"])
    checks["bwrap_features"] = {
        "passed": help_result.returncode == 0
        and all(flag in help_result.stdout for flag in ("--size", "--remount-ro", "--unshare-pid"))
    }
    guard = load_guard() if getattr(args, "profile", "raw") == "guarded" else None
    stack = contextlib.ExitStack()
    if guard:
        stack.enter_context(guard.private_binfmt())
        checks["private_binfmt_disabled"] = {"passed": True}
    try:
        linux_workspaces(root, toolchain, python, checks, environment, guard, stack)
    except (OSError, RuntimeError, ValueError, subprocess.SubprocessError) as exc:
        checks["workspace_probe_completed"] = {"passed": False, "error": str(exc)[-1000:]}
    finally:
        try:
            stack.close()
            if guard:
                checks["guarded_view_cleanup"] = {"passed": True}
        except (OSError, RuntimeError) as exc:
            checks["guarded_view_cleanup"] = {"passed": False, "error": str(exc)[-1000:]}
    return {"environment": environment, **outcome(checks)}


def linux_workspaces(root, toolchain, python, checks, environment, guard, stack):
    for workspace in (root / "repo 空格", root / "worktree 空格"):
        prefix = "repo" if workspace.name.startswith("repo") else "worktree"
        git_entry = workspace / ".git"
        protected = [
            workspace / ".env",
            workspace / ".agent/state",
            git_entry / "config" if git_entry.is_dir() else git_entry,
            root / "external/sentinel",
            root / "state/sentinel",
        ]
        snapshots = {path: path.read_bytes() for path in protected}
        # Linux symlink on the actual Windows filesystem, targeting unmounted state.
        try:
            (workspace / "escape-symlink").symlink_to(root / "external/sentinel")
            checks[prefix + ".symlink_setup"] = {"passed": True}
        except OSError as exc:
            checks[prefix + ".symlink_setup"] = {"passed": False, "errno": exc.errno}
        with socket.socket() as listener:
            listener.bind(("127.0.0.1", 0))
            listener.listen(1)
            port = listener.getsockname()[1]
            with socket.socket() as client:
                checks[prefix + ".listener_control"] = {
                    "passed": client.connect_ex(("127.0.0.1", port)) == 0,
                }
            env = {
                "PATH": "/usr/bin:/bin",
                "FIXLOOP_S0_FAKE_TOKEN": "fake",
                "WSL_INTEROP": "/run/WSL/fake-socket",
                "WSLENV": "FIXLOOP_S0_FAKE_TOKEN",
                "HTTP_PROXY": "http://127.0.0.1:1",
            }
            if guard:
                view = guard.FuseView(guard.RemoteBroker(), workspace.name)
                mapped = stack.enter_context(view.mounted())
                checks[prefix + ".guarded_view"] = {"passed": True}
                view_fs = run(["/usr/bin/stat", "-f", "-c", "%T", str(mapped)])
                environment["workspace_view_filesystem"] = view_fs.stdout.strip()
                checks[prefix + ".view_is_fuse"] = {
                    "passed": view_fs.returncode == 0
                    and view_fs.stdout.strip() in ("fuseblk", "fuse")
                }
            else:
                mapped = workspace
            argv = sandbox_arguments(mapped, toolchain, python, guarded=bool(guard))
            result = run(
                [
                    *argv,
                    "--",
                    "/toolchain/bin/python",
                    "-I",
                    "-c",
                    TARGET,
                    str(port),
                    str(root / "state"),
                    "guarded" if guard else "raw",
                ],
                env=env,
            )
        try:
            if result.returncode != 0:
                raise ValueError("target failed")
            observed = target_evidence(result.stdout, guarded=bool(guard))
            checks[prefix + ".sandbox_started"] = {"passed": True}
            checks.update({prefix + "." + key: value for key, value in observed.items()})
        except (ValueError, TypeError):
            checks[prefix + ".sandbox_started"] = {
                "passed": False,
                "exit_code": result.returncode,
                "stderr": result.stderr[-1000:],
            }
            continue
        persisted = workspace / "result.txt"
        checks[prefix + ".write_persisted"] = {
            "passed": persisted.exists() and persisted.read_text() == "persistent",
        }
        checks[prefix + ".source_unchanged"] = {
            "passed": (workspace / "source.py").read_text() == "value = 1\n",
        }
        checks[prefix + ".protected_state_unchanged"] = {
            "passed": all(path.read_bytes() == data for path, data in snapshots.items())
        }
        # Separate pytest call proves cwd mapping and persisted workspace visibility.
        test = run(
            [
                *argv,
                "--",
                "/toolchain/bin/python",
                "-I",
                "-m",
                "pytest",
                "-q",
                "-p",
                "no:cacheprovider",
                "test_sample.py",
            ],
            env=env,
        )
        checks[prefix + ".pytest"] = {
            "passed": test.returncode == 0 and "1 passed" in test.stdout,
            "exit_code": test.returncode,
            "stderr": test.stderr[-500:],
        }
    return {"environment": environment, **outcome(checks)}


def load_guard():
    # Fixed sibling source; never import from the user's working directory.
    spec = importlib.util.spec_from_file_location(
        "fixloop_s0_guard", Path(__file__).with_name("wsl_sandbox_s0_guard.py")
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def worker_evidence(stdout, *, guarded=False):
    observed = json.loads(stdout)
    if (
        not isinstance(observed, dict)
        or observed.get("schema") != SCHEMA
        or not isinstance(observed.get("environment", {}), dict)
        or not isinstance(observed.get("checks"), dict)
        or not observed["checks"]
        or any(
            not isinstance(value, dict) or type(value.get("passed")) is not bool
            for value in observed["checks"].values()
        )
    ):
        raise ValueError("invalid Linux evidence")
    required = {
        "supported_environment",
        "interop_control",
        "windows_mount_verified",
        "bwrap_features",
    }
    per_workspace = TARGET_CHECKS | {
        "symlink_setup",
        "listener_control",
        "sandbox_started",
        "write_persisted",
        "source_unchanged",
        "protected_state_unchanged",
        "pytest",
    }
    if guarded:
        required |= {"private_binfmt_disabled", "guarded_view_cleanup"}
        per_workspace |= GUARDED_CHECKS | {"guarded_view", "view_is_fuse"}
    required |= {f"{prefix}.{check}" for prefix in ("repo", "worktree") for check in per_workspace}
    # Early failure reports remain useful evidence. An all-positive report must
    # contain every required check; its own claimed decision is never trusted.
    if (
        all(value["passed"] for value in observed["checks"].values())
        and not required <= observed["checks"].keys()
    ):
        raise ValueError("missing Linux evidence")
    return observed


def broker_worker(command, root):
    """Bounded stdio RPC; broker credentials/fds never enter the target process."""
    broker = load_guard().WindowsBroker(root)
    process = subprocess.Popen(
        command,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    report, errors, stderr = [], [], []

    def drain():
        while chunk := process.stderr.read(4096):
            stderr.append(chunk)
            if sum(map(len, stderr)) > 16000:
                del stderr[:-1]

    def communicate():
        try:
            while True:
                line = process.stdout.readline(200000)
                if not line:
                    break
                if not line.endswith("\n"):
                    raise ValueError("oversized broker message")
                message = json.loads(line)
                if "rpc" not in message:
                    report.append(message)
                    continue
                try:
                    response = {"result": broker.call(message["rpc"])}
                except OSError as exc:
                    response = {"errno": exc.errno or 5}
                except (KeyError, ValueError, TypeError):
                    response = {"errno": 22}
                process.stdin.write(json.dumps(response) + "\n")
                process.stdin.flush()
        except Exception as exc:
            errors.append(exc)
        finally:
            process.stdin.close()

    drain_thread = threading.Thread(target=drain, daemon=True)
    rpc_thread = threading.Thread(target=communicate, daemon=True)
    drain_thread.start()
    rpc_thread.start()
    try:
        process.wait(timeout=120)
        rpc_thread.join(timeout=5)
        drain_thread.join(timeout=5)
        if rpc_thread.is_alive() or drain_thread.is_alive():
            raise RuntimeError("broker channel cleanup unconfirmed")
        if errors:
            raise RuntimeError("broker transport failed: " + str(errors[0]))
        if len(report) != 1:
            raise ValueError("missing or duplicate worker report")
        return subprocess.CompletedProcess(
            command, process.returncode, json.dumps(report[0]), "".join(stderr)
        )
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=5)
        # The worker also receives EOF and exits its private namespace. Do not
        # let an already-failed transport wait for its full execution deadline.
        for stream in (process.stdin, process.stdout, process.stderr):
            if not stream.closed and not rpc_thread.is_alive() and not drain_thread.is_alive():
                stream.close()
        broker.close()


def checked_git(root: Path, *arguments: str) -> None:
    # A user's GIT_DIR/WORK_TREE/INDEX_FILE must never redirect disposable
    # fixture operations into their real repository.
    env = {key: value for key, value in os.environ.items() if not key.upper().startswith("GIT_")}
    env.update({"GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.devnull})
    result = run(
        [
            "git",
            "-c",
            "core.hooksPath=" + os.devnull,
            "-c",
            "commit.gpgsign=false",
            "-c",
            "user.name=FixLoop S0",
            "-c",
            "user.email=s0@example.invalid",
            "-C",
            str(root),
            *arguments,
        ],
        env=env,
    )
    if result.returncode:
        raise RuntimeError("fixture git operation failed: " + result.stderr[-500:])


def create_fixtures(root: Path) -> dict:
    checks = {}
    external, state = root / "external", root / "state"
    external.mkdir()
    state.mkdir()
    (external / "sentinel").write_text(MARKER, encoding="utf-8")
    (state / "sentinel").write_text(MARKER, encoding="utf-8")
    repo, worktree = root / "repo 空格", root / "worktree 空格"
    repo.mkdir()
    (repo / "source.py").write_text("value = 1\n", encoding="utf-8")
    (repo / "中文 文件.txt").write_text("unicode", encoding="utf-8")
    (repo / "test_sample.py").write_text(
        "from pathlib import Path\ndef test_sample():\n"
        "    assert Path('source.py').read_text() == 'value = 1\\n'\n"
        "    assert Path('result.txt').read_text() == 'persistent'\n",
        encoding="utf-8",
    )
    checked_git(repo, "init", "--quiet", "--template=")
    checked_git(repo, "add", "source.py", "中文 文件.txt", "test_sample.py")
    checked_git(repo, "commit", "--quiet", "-m", "disposable S0 fixture")
    checked_git(repo, "worktree", "add", "--quiet", "--detach", str(worktree), "HEAD")
    for workspace, prefix in ((repo, "repo"), (worktree, "worktree")):
        (workspace / ".env").write_text(MARKER, encoding="utf-8")
        (workspace / ".agent").mkdir()
        (workspace / ".agent/state").write_text(MARKER, encoding="utf-8")
        if (workspace / ".git").is_dir():
            with (workspace / ".git/config").open("a", encoding="utf-8") as stream:
                stream.write("\n# " + MARKER + "\n")
        for name, target in (
            ("secret-alias", workspace / ".env"),
            ("outside-alias", external / "sentinel"),
        ):
            try:
                os.link(target, workspace / name)
                checks[prefix + "." + name + "_setup"] = {"passed": True}
            except OSError as exc:
                checks[prefix + "." + name + "_setup"] = {"passed": False, "errno": exc.errno}
        # PowerShell literals are single-quoted; paths cannot inject script text.
        link_literal = str(workspace / "escape-junction").replace("'", "''")
        target_literal = str(external).replace("'", "''")
        junction = run(
            ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", "-"],
            input_text="$ErrorActionPreference = 'Stop'\n"
            f"New-Item -ItemType Junction -Path '{link_literal}' "
            f"-Target '{target_literal}' | Out-Null\n",
        )
        checks[prefix + ".junction_setup"] = {"passed": junction.returncode == 0}
    return checks


def decode_wsl(data: bytes) -> str:
    # WSL management commands use UTF-16LE; Linux stdout uses UTF-8.
    return data.decode("utf-16-le" if b"\x00" in data else "utf-8", errors="replace")


def selected_wsl2(inventory: str, distribution: str) -> bool:
    for line in inventory.splitlines():
        fields = line.lstrip(" *\t").rsplit(None, 2)
        if len(fields) == 3 and fields[0].casefold() == distribution.casefold():
            return fields[2] == "2"
    return False


def cleanup_fixture(root: Path, parent: Path) -> None:
    resolved = root.resolve(strict=True)
    if (
        resolved.parent != parent.resolve(strict=True)
        or not resolved.name.startswith(".fixloop-s0-")
        or root.is_symlink()
        or getattr(root.lstat(), "st_file_attributes", 0)
        & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    ):
        raise RuntimeError("unsafe fixture cleanup target")

    def remove_readonly(function, path, exception_info):
        candidate = Path(path)
        if (
            not isinstance(exception_info[1], PermissionError)
            or not candidate.resolve(strict=True).is_relative_to(resolved)
            or not stat.S_ISREG(candidate.lstat().st_mode)
        ):
            raise exception_info[1]
        # Git objects have the Windows read-only attribute. Clear it only on a
        # verified regular file in our disposable fixture, then retry unlink.
        candidate.chmod(stat.S_IREAD | stat.S_IWRITE)
        function(path)

    shutil.rmtree(resolved, onerror=remove_readonly)


def windows_probe(args: argparse.Namespace) -> dict:
    parent = Path(args.workspace_parent).resolve(strict=True)
    if not parent.is_dir():
        raise ValueError("workspace parent must be a directory")
    wsl_path(parent)  # Reject UNC/device paths before creating anything.
    if not args.distribution or args.distribution.casefold() == "docker-desktop":
        raise ValueError("explicit non-Docker WSL2 distribution required")
    listing = subprocess.run(
        ["wsl.exe", "--list", "--verbose"], capture_output=True, timeout=20, check=False
    )
    checks = {"wsl_inventory": {"passed": listing.returncode == 0}}
    environment = {
        "windows": platform.version(),
        "distribution": args.distribution,
        "wsl_inventory": decode_wsl(listing.stdout).strip(),
    }
    if listing.returncode:
        checks["wsl_inventory"]["stderr"] = decode_wsl(listing.stderr)[-500:]
        return {"environment": environment, **outcome(checks)}
    checks["selected_wsl2"] = {
        "passed": selected_wsl2(
            environment["wsl_inventory"],
            args.distribution,
        )
    }
    if not checks["selected_wsl2"]["passed"]:
        return {"environment": environment, **outcome(checks)}
    # Cleanup stays in Python; never follow junction targets or delete user paths.
    root = Path(tempfile.mkdtemp(prefix=".fixloop-s0-", dir=parent))
    try:
        checks.update(create_fixtures(root))
        command = [
            "wsl.exe",
            "--distribution",
            args.distribution,
            "--exec",
            *(
                ["/usr/bin/unshare", "--user", "--map-root-user", "--mount"]
                if getattr(args, "profile", "raw") == "guarded"
                else []
            ),
            "/usr/bin/python3",
            "-I",
            wsl_path(Path(__file__).resolve()),
            "--linux-worker",
            "--fixture-root",
            wsl_path(root),
            "--toolchain",
            args.toolchain,
            "--profile",
            getattr(args, "profile", "raw"),
        ]
        result = (
            broker_worker(command, root)
            if getattr(args, "profile", "raw") == "guarded"
            else run(command, timeout=120)
        )
        if result.returncode not in (0, 2):
            checks["linux_probe"] = {
                "passed": False,
                "exit_code": result.returncode,
                "stderr": result.stderr[-1000:],
            }
        else:
            observed = worker_evidence(
                result.stdout, guarded=getattr(args, "profile", "raw") == "guarded"
            )
            checks.update(observed["checks"])
            environment.update(observed.get("environment", {}))
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as exc:
        checks["probe_completed"] = {"passed": False, "error": str(exc)[-1000:]}
    finally:
        # Resolve/verify the exact created root before recursive cleanup. rmtree
        # on supported Windows Python removes junctions without their targets.
        try:
            cleanup_fixture(root, parent)
            checks["fixture_cleanup"] = {"passed": not root.exists()}
        except (OSError, RuntimeError) as exc:
            checks["fixture_cleanup"] = {"passed": False, "error": str(exc)[-500:]}
    return {"environment": environment, **outcome(checks)}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--distribution", default="Ubuntu")
    parser.add_argument("--workspace-parent", default=".")
    parser.add_argument(
        "--toolchain",
        required=True,
        help="existing trusted Linux Python 3.14 venv containing pytest",
    )
    parser.add_argument("--output", type=Path)
    parser.add_argument("--profile", choices=["raw", "guarded"], default="guarded")
    parser.add_argument("--linux-worker", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--fixture-root", help=argparse.SUPPRESS)
    args = parser.parse_args()
    try:
        if args.linux_worker:
            if sys.platform != "linux" or not args.fixture_root:
                raise ValueError("Linux worker requires fixture root")
            result = linux_probe(args)
        elif sys.platform == "win32":
            result = windows_probe(args)
        else:
            raise ValueError("launch this diagnostic on Windows")
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as exc:
        result = outcome({"probe_completed": {"passed": False, "error": str(exc)[-1000:]}})
    report = {
        "schema": SCHEMA,
        "profile": args.profile,
        "diagnostic_only": True,
        "product_enabled": False,
        **result,
    }
    serialized = (
        json.dumps(
            report,
            ensure_ascii=False,
            indent=None if args.linux_worker and args.profile == "guarded" else 2,
        )
        + "\n"
    )
    if args.output:
        args.output.write_text(serialized, encoding="utf-8")
    sys.stdout.buffer.write(serialized.encode("utf-8"))
    return 0 if report["probe_passed"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
