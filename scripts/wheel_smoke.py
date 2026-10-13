"""Build from a fresh source tree, install its wheel and run offline evaluation."""

import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path


def run(*args, cwd, env=None):
    environment = {**os.environ, "PIP_NO_INDEX": "1", "PIP_DISABLE_PIP_VERSION_CHECK": "1"}
    if env:
        environment.update(env)
    subprocess.run(args, cwd=cwd, env=environment, check=True, timeout=180)


def main():
    repository = Path(__file__).resolve().parents[1]
    (repository / ".tmp").mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="wheel-smoke-", dir=repository / ".tmp") as directory:
        task = Path(directory).resolve()
        source, installed, smoke = task / "source", task / "installed", task / "smoke"
        source.mkdir()
        smoke.mkdir()
        # Copy only current source/resources. No build, egg-info, cached wheels or results.
        for name in ("agent_runtime", "src"):
            shutil.copytree(
                repository / name,
                source / name,
                ignore=shutil.ignore_patterns(
                    "__pycache__", "*.pyc", "*.egg-info", ".pytest_cache", ".agent"
                ),
            )
        shutil.copy2(repository / "pyproject.toml", source / "pyproject.toml")
        run(
            sys.executable,
            "-m",
            "pip",
            "wheel",
            ".",
            "--no-deps",
            "--no-build-isolation",
            "--wheel-dir",
            str(task / "dist"),
            cwd=source,
        )
        wheels = list((task / "dist").glob("*.whl"))
        if len(wheels) != 1:
            raise RuntimeError("clean build must produce exactly one wheel")
        run(
            sys.executable,
            "-m",
            "pip",
            "install",
            "--no-deps",
            "--no-compile",
            "--target",
            str(installed),
            str(wheels[0]),
            cwd=smoke,
        )
        # -I ignores PYTHONPATH and CWD; only the installed wheel is inserted explicitly.
        probe = """
import json, pathlib, sys
sys.path.insert(0, sys.argv[1])
from src.eval.case_io import DEFAULT_CASES_DIR, OFFLINE_SMOKE_CASES, read_expected_patch
from src.eval.fake_runner import fixture_repair_service_factory
from src.eval.runner import EvalRunner
import src.repair.service
assert pathlib.Path(src.repair.service.__file__).is_relative_to(pathlib.Path(sys.argv[1]))
ids = list(OFFLINE_SMOKE_CASES)
for case in ids:
    assert read_expected_patch(DEFAULT_CASES_DIR.joinpath(case))
runner = EvalRunner(fixture_repair_service_factory(DEFAULT_CASES_DIR),
                    cases_dir=DEFAULT_CASES_DIR, output_dir=sys.argv[2])
report = runner.run_all(ids)
assert all(case.fixed for case in report.cases), [(c.case_id, c.error) for c in report.cases]
print(json.dumps({'wheel_offline_smoke': 'passed', 'cases': ids}))
"""
        env = {
            **os.environ,
            "HF_HUB_OFFLINE": "1",
            "TRANSFORMERS_OFFLINE": "1",
            "GIT_CEILING_DIRECTORIES": str(task),
            "FIXLOOP_STATE_ROOT": str(task / "state"),
        }
        run(
            sys.executable,
            "-I",
            "-c",
            probe,
            str(installed),
            str(smoke / "results"),
            cwd=smoke,
            env=env,
        )


if __name__ == "__main__":
    main()
