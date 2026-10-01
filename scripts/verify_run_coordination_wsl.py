"""Run selected native sandbox cases in an isolated ext4 fixture under WSL.

Example (inside WSL):
  /usr/bin/python3 scripts/verify_run_coordination_wsl.py \
    --fixture-base /home/haoyu/fixloop-sandbox-p0 \
    tests/test_run_coordination_wsl.py::test_owner_envelope_and_historical_receipts_are_fenced
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import tempfile
from pathlib import Path


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--fixture-base", type=Path, required=True)
    parser.add_argument("--source", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--toolchain", type=Path)
    parser.add_argument("pytest_args", nargs=argparse.REMAINDER)
    args = parser.parse_args()
    base = args.fixture_base.resolve(strict=True)
    source = args.source.resolve(strict=True)
    toolchain = (args.toolchain or base / "toolchain").resolve(strict=True)
    python = toolchain / "bin/python"
    if os.name != "posix" or not python.is_file():
        parser.error("run inside WSL with an existing Python/pytest toolchain")
    selection = args.pytest_args
    if selection[:1] == ["--"]:
        selection = selection[1:]
    if not selection:
        selection = ["tests/test_run_coordination_wsl.py", "tests/test_linux_sandbox_lifecycle.py"]
    with tempfile.TemporaryDirectory(prefix="fixloop-coordination-", dir=base) as temporary:
        root = Path(temporary)
        controller = root / "controller"
        controller.mkdir()
        for name in ("agent_runtime", "src", "tests"):
            shutil.copytree(
                source / name,
                controller / name,
                ignore=shutil.ignore_patterns("__pycache__", "*.pyc", "conftest.py"),
            )
        (root / "toolchain").symlink_to(toolchain, target_is_directory=True)
        env = dict(
            os.environ,
            FIXLOOP_P1_ROOT=str(root),
            FIXLOOP_P1_CONTROLLER_ROOT=str(controller),
            PYTEST_DISABLE_PLUGIN_AUTOLOAD="1",
            PYTHONPATH=str(controller),
        )
        # Sandbox modules need only stdlib; do not import high-level model dependencies.
        bootstrap = (
            "import sys,types,pathlib; p=types.ModuleType('agent_runtime'); "
            "p.__path__=[str(pathlib.Path.cwd()/'agent_runtime')]; "
            "sys.modules['agent_runtime']=p; import pytest; "
            "raise SystemExit(pytest.main(sys.argv[1:]))"
        )
        command = [
            str(python),
            "-c",
            bootstrap,
            *selection,
            "-q",
            "-p",
            "no:cacheprovider",
            "--basetemp",
            str(root / "pytest"),
        ]
        print(f"Isolated WSL fixture: {root}", flush=True)
        return subprocess.run(command, cwd=controller, env=env, check=False).returncode


if __name__ == "__main__":
    raise SystemExit(main())
