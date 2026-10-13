"""Owned host test processes shared by repair and evaluation.

Cancellation is acknowledged only after the process scope is empty. Output is
spooled to files so a descendant retaining a pipe cannot strand a reader thread.
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from agent_runtime.cancellation import CancelledError
from agent_runtime.process_scope import CleanupUnconfirmedError, WindowsJob


class ProcessCleanupUnconfirmedError(CleanupUnconfirmedError):
    """The caller must retain its workspace and prohibit rollback/reuse."""


def _resume_windows(proc):
    import ctypes
    from ctypes import wintypes

    resume = ctypes.WinDLL("ntdll").NtResumeProcess
    resume.argtypes = [wintypes.HANDLE]
    resume.restype = ctypes.c_long
    if resume(int(proc._handle)) != 0:
        raise OSError("cannot resume owned test process")


def _group_empty(pgid):
    # Linux zombies cannot mutate files; an inaccessible process is unknown.
    proc_root = Path("/proc")
    if proc_root.is_dir():
        for path in proc_root.iterdir():
            if not path.name.isdigit():
                continue
            try:
                text = (path / "stat").read_text()
                fields = text[text.rfind(")") + 2 :].split()
                if int(fields[2]) == pgid and fields[0] != "Z":
                    return False
            except FileNotFoundError:
                continue
            except (OSError, ValueError, IndexError) as exc:
                raise ProcessCleanupUnconfirmedError(
                    "process group inspection unavailable"
                ) from exc
        return True
    try:
        os.killpg(pgid, 0)
    except ProcessLookupError:
        return True
    return False


def _stop_tree(proc, job, timeout=3):
    try:
        if job is not None:
            job.terminate(timeout=timeout)
        else:
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        proc.wait(timeout=timeout)
        deadline = time.monotonic() + timeout
        while not (job.empty() if job else _group_empty(proc.pid)):
            if time.monotonic() >= deadline:
                raise ProcessCleanupUnconfirmedError("test process tree still active")
            time.sleep(0.02)
    except (OSError, subprocess.TimeoutExpired, CleanupUnconfirmedError) as exc:
        raise ProcessCleanupUnconfirmedError(str(exc)) from exc


def run_command(command, repo, *, cancel_token=None, timeout_s=None):
    """Return (exit code, output) only after the owned process tree has stopped."""
    if cancel_token is not None:
        cancel_token.check()
    if sys.platform == "linux":
        return _run_linux(command, repo, cancel_token=cancel_token, timeout_s=timeout_s)
    started = time.monotonic()
    job = WindowsJob() if os.name == "nt" else None
    proc = None
    attached = False
    popen_command = command
    if (
        os.name == "nt"
        and isinstance(command, (list, tuple))
        and len(command) == 5
        and Path(command[0]).name.lower() == "cmd.exe"
        and list(command[1:4]) == ["/d", "/s", "/c"]
    ):
        # cmd /c uses shell quoting, not the escaped quotes used by the CRT.
        popen_command = subprocess.list2cmdline(command[:4]) + ' "' + command[4] + '"'
    try:
        with tempfile.TemporaryFile() as stdout, tempfile.TemporaryFile() as stderr:
            proc = subprocess.Popen(
                popen_command,
                cwd=repo,
                stdout=stdout,
                stderr=stderr,
                creationflags=(0x4 | subprocess.CREATE_NO_WINDOW) if job else 0,
                start_new_session=job is None,
            )
            try:
                if job:
                    job.attach(proc.pid)
                    attached = True
                    _resume_windows(proc)
                while proc.poll() is None:
                    if cancel_token is not None:
                        cancel_token.check()
                    if timeout_s is not None and time.monotonic() - started >= timeout_s:
                        raise subprocess.TimeoutExpired(command, timeout_s)
                    time.sleep(0.02)
                code = proc.returncode
                # Even a successful parent may leave a writer behind.
                _stop_tree(proc, job)
                if cancel_token is not None:
                    cancel_token.check()
            except BaseException as exc:
                if job and not attached:
                    proc.kill()  # Failed assignment: the suspended process never executed code.
                _stop_tree(proc, job)
                if job is None and isinstance(exc, (CancelledError, subprocess.TimeoutExpired)):
                    # A process can leave its POSIX group with setsid(). Group
                    # emptiness alone cannot certify a stopped descendant tree.
                    raise ProcessCleanupUnconfirmedError(
                        "POSIX group stopped; descendant containment unavailable"
                    ) from exc
                raise
            stdout.seek(0)
            stderr.seek(0)
            output = (stdout.read() + stderr.read()).decode("utf-8", errors="replace")
            return code, output
    finally:
        if job:
            job.close()


def _run_linux(command, repo, *, cancel_token=None, timeout_s=None):
    started = time.monotonic()
    with tempfile.TemporaryDirectory(prefix="fixloop-test-") as directory:
        receipt = Path(directory) / "receipt.json"
        with tempfile.TemporaryFile() as stdout, tempfile.TemporaryFile() as stderr:
            proc = subprocess.Popen(
                [
                    sys.executable,
                    "-I",
                    str(Path(__file__).with_name("linux_test_supervisor.py")),
                    str(receipt),
                    *map(str, command),
                ],
                cwd=repo,
                stdin=subprocess.PIPE,
                stdout=stdout,
                stderr=stderr,
                start_new_session=True,
            )
            interrupted = None
            try:
                while proc.poll() is None:
                    if cancel_token is not None:
                        cancel_token.check()
                    if timeout_s is not None and time.monotonic() - started >= timeout_s:
                        raise subprocess.TimeoutExpired(command, timeout_s)
                    time.sleep(0.02)
            except BaseException as exc:
                interrupted = exc
                proc.stdin.close()
                try:
                    proc.wait(timeout=5)
                except subprocess.TimeoutExpired as error:
                    raise ProcessCleanupUnconfirmedError("Linux supervisor did not stop") from error
            finally:
                if not proc.stdin.closed:
                    proc.stdin.close()
            try:
                result = json.loads(receipt.read_text(encoding="utf-8"))
            except (OSError, ValueError) as exc:
                raise ProcessCleanupUnconfirmedError("Linux cleanup receipt unavailable") from exc
            if result.get("cleanup") != "confirmed":
                raise ProcessCleanupUnconfirmedError(result.get("error", "cleanup unconfirmed"))
            if interrupted is not None:
                raise interrupted
            if result.get("error") or result.get("exit_code") is None:
                raise OSError(result.get("error", "command start failed"))
            if cancel_token is not None:
                cancel_token.check()
            stdout.seek(0)
            stderr.seek(0)
            return result["exit_code"], (stdout.read() + stderr.read()).decode("utf-8", "replace")


def project_python(repo):
    root = Path(repo)
    suffix = "Scripts/python.exe" if os.name == "nt" else "bin/python"
    return next(
        (
            str((root / name / suffix).resolve())
            for name in (".venv", "venv")
            if (root / name / suffix).is_file()
        ),
        sys.executable,
    )


def run_pytest(repo, test_files=None, *, cancel_token=None, timeout_s=None):
    targets = [str(p) for p in (test_files or []) if str(p).strip()]
    return run_command(
        [project_python(repo), "-m", "pytest", "-q", *targets],
        Path(repo),
        cancel_token=cancel_token,
        timeout_s=timeout_s,
    )
