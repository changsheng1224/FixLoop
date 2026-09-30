"""Explicit Windows-to-WSL transport test against the dedicated P0 fixture."""

import os
import subprocess
import uuid

import pytest

from agent_runtime.linux_sandbox import SandboxRequest, WindowsWslBackend, WslLauncherConfig


@pytest.mark.skipif(os.environ.get("FIXLOOP_P2_LIVE") != "1", reason="explicit WSL fixture only")
def test_windows_launcher_executes_and_verifies_with_receipts():
    run_id = uuid.uuid4().hex
    base = "/home/haoyu/fixloop-sandbox-p0"
    controller = base + "/controller"

    def fixture_command(script):
        return subprocess.run(
            [
                "wsl.exe",
                "--distribution",
                "Ubuntu",
                "--exec",
                "/usr/bin/python3",
                "-c",
                script,
                run_id,
            ],
            capture_output=True,
            text=True,
            check=True,
            timeout=60,
        )

    config_path = fixture_command(
        "import json,sys; from pathlib import Path; "
        f"base=Path({base!r}); controller=Path({controller!r}); "
        "root=base/('p2-live-'+sys.argv[1]); "
        "(root/'workspace').mkdir(parents=True,mode=0o700); "
        "(root/'state').mkdir(mode=0o700); "
        "config=controller/('p2-live-'+sys.argv[1]+'.json'); "
        f"config.write_text(json.dumps(dict(workspace=str(root/'workspace'),state_root=str(root/'state'),toolchain=str(base/'toolchain'),helper=str(controller/'agent_runtime/linux_sandbox/supervisor.py')))); "
        "config.chmod(0o600); print(config)"
    ).stdout.strip()
    try:
        backend = WindowsWslBackend(
            WslLauncherConfig(
                "Ubuntu",
                base + "/toolchain/bin/python",
                controller + "/agent_runtime/linux_sandbox/controller_entry.py",
                config_path,
            )
        )
        write = backend.execute(
            SandboxRequest(
                "ws",
                "task",
                run_id,
                "p2-win-write-" + run_id,
                "command",
                (
                    "/toolchain/bin/python",
                    "-I",
                    "-c",
                    "from pathlib import Path; Path('test_small.py').write_text('def test_ok():\\n    assert True\\n')",
                ),
                timeout_s=20,
            )
        )
        assert write.execution_status == "completed" and write.exit_code == 0, write
        assert write.cleanup == "confirmed" and write.actual_backend == "linux_sandbox"
        verify = backend.execute(
            SandboxRequest(
                "ws",
                "task",
                run_id,
                "p2-win-pytest-" + run_id,
                "pytest",
                ("/toolchain/bin/python", "-I", "-m", "pytest", "test_small.py", "-q"),
                timeout_s=60,
            )
        )
        assert verify.execution_status == "completed" and verify.exit_code == 0, verify
        assert verify.cleanup == "confirmed" and "1 passed" in verify.stdout_excerpt
        assert verify.receipt_id != write.receipt_id
    finally:
        fixture_command(
            "import shutil,sys; from pathlib import Path; "
            f"base=Path({base!r}).resolve(); controller=Path({controller!r}).resolve(); "
            "root=(base/('p2-live-'+sys.argv[1])).resolve(); "
            "config=(controller/('p2-live-'+sys.argv[1]+'.json')).resolve(); "
            "assert root != base and root.is_relative_to(base) and config.is_relative_to(controller); "
            "shutil.rmtree(root); config.unlink()"
        )
