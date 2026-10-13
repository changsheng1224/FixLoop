"""A successful command cannot leave a detached writer behind."""

import subprocess
import sys
import threading
import time

import pytest

from agent_runtime.cancellation import CancellationToken, CancelledError
from agent_runtime.test_execution import run_command

pytestmark = pytest.mark.skipif(sys.platform != "linux", reason="Linux subreaper")


@pytest.mark.parametrize("ending", ["success", "timeout", "cancel"])
def test_detached_double_fork_stopped(tmp_path, ending):
    child = (
        "import os,time,pathlib; os.setsid(); "
        "pid=os.fork(); os._exit(0) if pid else None; "
        "time.sleep(1); pathlib.Path('late').write_text('bad')"
    )
    parent = (
        "import os,sys,time,pathlib; "
        "pid=os.fork(); "
        f"exec({child!r}) if pid == 0 else None; "
        "pathlib.Path('ready').write_text('ok'); "
        + ("sys.exit(0)" if ending == "success" else "time.sleep(30)")
    )
    token = CancellationToken()
    if ending == "cancel":
        timer = threading.Timer(0.3, token.cancel)
        timer.start()
    try:
        if ending == "success":
            assert run_command([sys.executable, "-c", parent], tmp_path)[0] == 0
        else:
            expected = CancelledError if ending == "cancel" else subprocess.TimeoutExpired
            with pytest.raises(expected):
                run_command(
                    [sys.executable, "-c", parent],
                    tmp_path,
                    cancel_token=token,
                    timeout_s=0.3 if ending == "timeout" else 5,
                )
        assert (tmp_path / "ready").exists()
        time.sleep(1.1)
        assert not (tmp_path / "late").exists()
    finally:
        if ending == "cancel":
            timer.join()
