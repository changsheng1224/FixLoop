"""Real OS process-tree cleanup, including a detached child after parent exit."""

import os
import subprocess
import sys

import pytest

from agent_runtime.plan_runtime.processes import confirmed_exited, process_identity
from src.workbench.process_scope import WindowsJob, reconcile_scope


@pytest.mark.skipif(os.name != "nt", reason="Windows Job Object acceptance")
def test_job_contains_detached_descendant_after_parent_exit(tmp_path):
    marker = tmp_path / "late.txt"
    script = """
import subprocess, sys
sys.stdin.readline()
child = subprocess.Popen([sys.executable, '-c', sys.argv[1], sys.argv[2]], creationflags=subprocess.CREATE_NEW_PROCESS_GROUP)
print(child.pid, flush=True)
"""
    child_script = (
        "import pathlib,sys,time;time.sleep(3);pathlib.Path(sys.argv[1]).write_text('late')"
    )
    scope = WindowsJob()
    process = subprocess.Popen(
        [sys.executable, "-c", script, child_script, str(marker)],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        text=True,
        creationflags=subprocess.CREATE_NO_WINDOW,
    )
    try:
        scope.attach(process.pid)
        process.stdin.write("go\n")
        process.stdin.flush()
        child_pid = int(process.stdout.readline())
        identity = process_identity(child_pid)
        assert identity and identity.get("generation") != "unknown"
        process.wait(timeout=5)
        assert not scope.empty()
        scope.terminate()
        assert scope.empty() and confirmed_exited(identity)
        assert not marker.exists()
    finally:
        scope.close()
        process.wait(timeout=5)
        process.stdin.close()
        process.stdout.close()


@pytest.mark.skipif(os.name != "nt", reason="Windows durable scope recovery")
def test_recovery_uses_named_scope_and_process_generation():
    scope = WindowsJob()
    process = subprocess.Popen(
        [sys.executable, "-c", "import time;time.sleep(30)"],
        creationflags=subprocess.CREATE_NO_WINDOW,
    )
    try:
        scope.attach(process.pid)
        descriptor = {**scope.descriptor(), "worker": process_identity(process.pid)}
        assert reconcile_scope(descriptor)
        process.wait(timeout=5)
        assert scope.empty()
        scope.close()
        assert reconcile_scope(descriptor)
        assert not reconcile_scope({"backend": "unknown", "worker": descriptor["worker"]})
    finally:
        scope.close()
        process.wait(timeout=5)
