"""Diagnostic-only WSL/bwrap probe; not a production sandbox policy."""

import json
import os
import signal
import socket
import subprocess
import tempfile
import time
from pathlib import Path


def main() -> None:
    with tempfile.TemporaryDirectory(prefix="fixloop-p0-", dir="/var/tmp") as temporary:
        root = Path(temporary)
        workspace = root / "workspace"
        state = root / "state"
        workspace.mkdir()
        state.mkdir()
        (state / "sentinel").write_text("private-p0-sentinel", encoding="ascii")
        (workspace / "escape").symlink_to(state / "sentinel")
        (workspace / ".git").mkdir()
        (workspace / ".git" / "config").write_text("private-git-config", encoding="ascii")

        # /usr is mounted for this diagnostic only. P1 must use a fixed toolchain closure.
        base = [
            "/usr/bin/bwrap",
            "--unshare-user",
            "--unshare-pid",
            "--unshare-net",
            "--unshare-ipc",
            "--unshare-uts",
            "--die-with-parent",
            "--new-session",
            "--clearenv",
            "--ro-bind",
            "/usr",
            "/usr",
            "--ro-bind",
            "/lib",
            "/lib",
            "--ro-bind",
            "/lib64",
            "/lib64",
            "--bind",
            str(workspace),
            "/workspace",
            "--tmpfs",
            "/workspace/.git",
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
            "HOME",
            "/home/sandbox",
            "--setenv",
            "PATH",
            "/usr/bin:/bin",
            "--chdir",
            "/workspace",
            "--",
            "/usr/bin/python3",
            "-I",
            "-c",
        ]

        def run(code: str, *args: str) -> dict:
            try:
                proc = subprocess.run(
                    [*base, code, *args],
                    capture_output=True,
                    text=True,
                    timeout=15,
                    env={"PATH": "/usr/bin:/bin"},
                    check=False,
                )
                return {
                    "exit": proc.returncode,
                    "stdout": proc.stdout.strip(),
                    "stderr": proc.stderr.strip()[-1000:],
                }
            except (OSError, subprocess.TimeoutExpired) as exc:
                return {"error": type(exc).__name__, "detail": str(exc)[:300]}

        checks = {}
        checks["mounts_and_interop"] = run(
            "import json,os,pathlib; "
            "p=pathlib.Path; "
            "(p('/workspace')/'write-test').write_text('ok'); "
            "paths=['/workspace/../state/sentinel','/workspace/escape',"
            "'/mnt/c/Windows/System32/cmd.exe','/proc/sys/fs/binfmt_misc/WSLInterop',"
            "'/run/WSL','/workspace/.git/config']; "
            "print(json.dumps({'pid':os.getpid(),'paths':{x:p(x).exists() for x in paths},"
            "'home':p('/home/sandbox').exists(), 'env':sorted(os.environ),"
            "'workspace_write':p('/workspace/write-test').read_text()}))"
        )
        checks["workspace_write_persisted"] = (
            (workspace / "write-test").read_text() == "ok"
            if (workspace / "write-test").exists()
            else False
        )

        with socket.socket() as listener:
            listener.bind(("127.0.0.1", 0))
            listener.listen(1)
            port = listener.getsockname()[1]
            with socket.socket() as client:
                checks["host_listener_reachable"] = client.connect_ex(("127.0.0.1", port)) == 0
            checks["sandbox_listener_connection"] = run(
                "import socket,sys; s=socket.socket(); s.settimeout(2); "
                "print(s.connect_ex(('127.0.0.1',int(sys.argv[1]))))",
                str(port),
            )

        checks["tmpfs_limit"] = run(
            "import os; print(os.statvfs('/tmp').f_blocks*os.statvfs('/tmp').f_frsize); "
            "open('/tmp/overflow','wb').write(b'x'*2097152)"
        )
        checks["toolchain_read_only"] = run("open('/usr/bin/p0-write-test','wb').write(b'x')")
        (workspace / "test_probe.py").write_text(
            "from pathlib import Path\n"
            "def test_ok():\n"
            "    assert not Path('/home/haoyu/fixloop-sandbox-p0/state').exists()\n"
            "    assert not Path('/workspace/.git/config').exists()\n",
            encoding="ascii",
        )
        lib_dir = Path("/usr/lib/x86_64-linux-gnu")
        libraries = (
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
        narrow = [
            "/usr/bin/bwrap",
            "--unshare-user",
            "--unshare-pid",
            "--unshare-net",
            "--die-with-parent",
            "--clearenv",
            "--dir",
            "/usr",
            "--dir",
            "/usr/bin",
            "--dir",
            "/usr/lib",
            "--dir",
            str(lib_dir),
            "--dir",
            "/lib64",
            "--ro-bind",
            "/usr/bin/python3.14",
            "/usr/bin/python3.14",
            "--symlink",
            "python3.14",
            "/usr/bin/python3",
            "--ro-bind",
            "/usr/lib/python3.14",
            "/usr/lib/python3.14",
            "--ro-bind",
            str(lib_dir / "ld-linux-x86-64.so.2"),
            "/lib64/ld-linux-x86-64.so.2",
        ]
        for name in libraries:
            narrow.extend(("--ro-bind", str(lib_dir / name), str(lib_dir / name)))
        narrow.extend(
            (
                "--ro-bind",
                "/home/haoyu/fixloop-sandbox-p0/toolchain",
                "/toolchain",
                "--bind",
                str(workspace),
                "/workspace",
                "--tmpfs",
                "/workspace/.git",
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
                "PYTEST_DISABLE_PLUGIN_AUTOLOAD",
                "1",
                "--chdir",
                "/workspace",
                "--",
                "/toolchain/bin/python",
                "-I",
                "-m",
                "pytest",
                "-q",
                "test_probe.py",
            )
        )
        try:
            narrow_result = subprocess.run(
                narrow,
                capture_output=True,
                text=True,
                timeout=20,
                env={"PATH": "/usr/bin:/bin"},
                check=False,
            )
            checks["narrow_pytest"] = {
                "exit": narrow_result.returncode,
                "stdout": narrow_result.stdout.strip()[-1000:],
                "stderr": narrow_result.stderr.strip()[-1000:],
            }
        except (OSError, subprocess.TimeoutExpired) as exc:
            checks["narrow_pytest"] = {"error": type(exc).__name__, "detail": str(exc)[:300]}
        limit_argv = narrow[: narrow.index("--")]
        limit_argv[limit_argv.index("1048576")] = "67108864"
        limit_argv.extend(
            (
                "--",
                "/toolchain/bin/python",
                "-I",
                "-c",
                "import os; print(os.statvfs('/home/sandbox').f_blocks*"
                "os.statvfs('/home/sandbox').f_frsize); "
                "f=open('/home/sandbox/overflow','wb'); "
                "[f.write(b'x'*1048576) for _ in range(65)]",
            )
        )
        try:
            limit_result = subprocess.run(
                limit_argv,
                capture_output=True,
                text=True,
                timeout=20,
                env={"PATH": "/usr/bin:/bin"},
                check=False,
            )
            checks["profile_tmpfs_limit"] = {
                "exit": limit_result.returncode,
                "stdout": limit_result.stdout.strip(),
                "stderr": limit_result.stderr.strip()[-500:],
            }
        except (OSError, subprocess.TimeoutExpired) as exc:
            checks["profile_tmpfs_limit"] = {"error": type(exc).__name__, "detail": str(exc)[:300]}
        heartbeat = workspace / "heartbeat"
        child_code = (
            "import os,time,pathlib; os.setsid(); p=pathlib.Path('/workspace/heartbeat'); "
            "end=time.monotonic()+4; "
            "exec('while time.monotonic()<end: "
            "p.write_text(str(time.monotonic())); time.sleep(.1)')"
        )
        parent_code = (
            "import subprocess,time; "
            "subprocess.Popen(['/usr/bin/python3','-I','-c'," + repr(child_code) + "]); "
            "time.sleep(4)"
        )
        proc = subprocess.Popen(
            [*base, parent_code],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            env={"PATH": "/usr/bin:/bin"},
        )
        try:
            deadline = time.monotonic() + 2
            while not heartbeat.exists() and proc.poll() is None and time.monotonic() < deadline:
                time.sleep(0.05)
            started = heartbeat.exists()
            before = heartbeat.read_text() if started else ""
            proc.send_signal(signal.SIGKILL)
            proc.wait(timeout=2)
            time.sleep(0.5)
            checks["parent_exit_heartbeat_stopped"] = {
                "started": started,
                "stopped": started and heartbeat.read_text() == before,
            }
        finally:
            if proc.poll() is None:
                proc.kill()
                proc.wait(timeout=2)
        mount = checks["mounts_and_interop"]
        visible = json.loads(mount["stdout"]) if mount.get("exit") == 0 else {}
        passed = (
            checks["workspace_write_persisted"]
            and visible.get("pid") == 2
            and visible.get("workspace_write") == "ok"
            and visible.get("paths")
            and not any(visible["paths"].values())
            and checks["host_listener_reachable"]
            and checks["sandbox_listener_connection"].get("stdout") not in (None, "", "0")
            and checks["tmpfs_limit"].get("stdout") == "1048576"
            and "Errno 28" in checks["tmpfs_limit"].get("stderr", "")
            and "Errno 30" in checks["toolchain_read_only"].get("stderr", "")
            and checks["narrow_pytest"].get("exit") == 0
            and "1 passed" in checks["narrow_pytest"].get("stdout", "")
            and checks["profile_tmpfs_limit"].get("stdout") == "67108864"
            and "Errno 28" in checks["profile_tmpfs_limit"].get("stderr", "")
            and checks["parent_exit_heartbeat_stopped"]["stopped"]
        )
        print(
            json.dumps(
                {
                    "diagnostic_only": True,
                    "kernel": os.uname().release,
                    "workspace_filesystem": subprocess.run(
                        ["/usr/bin/stat", "-f", "-c", "%T", str(workspace)],
                        capture_output=True,
                        text=True,
                        check=False,
                    ).stdout.strip(),
                    "checks": checks,
                    "probe_passed": bool(passed),
                },
                indent=2,
                sort_keys=True,
            )
        )
        if not passed:
            raise SystemExit(1)


if __name__ == "__main__":
    main()
