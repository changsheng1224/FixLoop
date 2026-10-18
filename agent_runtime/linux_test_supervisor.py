"""Isolated subreaper for host commands, including double-forked descendants."""

from __future__ import annotations

import ctypes
import json
import os
import selectors
import signal
import subprocess
import sys
import time
from pathlib import Path


def _children() -> list[int]:
    return [
        int(pid)
        for pid in Path(f"/proc/{os.getpid()}/task/{os.getpid()}/children").read_text().split()
    ]


def _drain(proc: subprocess.Popen, timeout: float = 3) -> None:
    # Killing a parent adopts its orphans into this dedicated subreaper. Repeat
    # until every generation has been killed and reaped, independent of setsid.
    deadline = time.monotonic() + timeout
    while True:
        children = _children()
        if not children:
            return
        for pid in children:
            try:
                os.kill(pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        proc.poll()
        while True:
            try:
                pid, _ = os.waitpid(-1, os.WNOHANG)
            except ChildProcessError:
                break
            if pid == 0:
                break
        if time.monotonic() >= deadline:
            raise RuntimeError("descendant cleanup unconfirmed")
        time.sleep(0.01)


def main() -> None:
    receipt = Path(sys.argv[1])
    result = {"cleanup": "unknown", "exit_code": None}
    proc = None
    try:
        libc = ctypes.CDLL(None, use_errno=True)
        if libc.prctl(36, 1, 0, 0, 0) != 0:  # PR_SET_CHILD_SUBREAPER
            raise OSError(ctypes.get_errno(), "subreaper unavailable")
        proc = subprocess.Popen(sys.argv[2:], stdin=subprocess.DEVNULL, start_new_session=True)
        with selectors.DefaultSelector() as selector:
            selector.register(sys.stdin, selectors.EVENT_READ)
            while proc.poll() is None:
                if selector.select(0.02):
                    break
        result["exit_code"] = proc.poll()
        _drain(proc)
        result["cleanup"] = "confirmed"
    except BaseException as exc:
        result["error"] = str(exc)
        if proc is not None:
            try:
                _drain(proc)
                result["cleanup"] = "confirmed"
            except Exception:
                pass
    finally:
        receipt.write_text(json.dumps(result), encoding="utf-8")


if __name__ == "__main__":
    main()
