"""Execute declarative verification steps with structured failure categories."""

from __future__ import annotations

import shutil
import subprocess
import time
from pathlib import Path

from src.repair.verification.verification_profiles import VerificationProfile


def run_profile(
    repo_root: str | Path,
    profile: VerificationProfile,
    *,
    target: str = "",
    run_tests: bool = True,
) -> dict:
    root = Path(repo_root)
    steps = list(profile.static_steps) + (list(profile.test_steps) if run_tests else [])
    results = []
    for step in steps:
        command = list(step.command)
        if target and step.phase == "target_tests" and profile.language == "python":
            command.append(target)
        executable = shutil.which(command[0])
        if executable is None:
            return {
                "all_passed": False,
                "category": "verification_environment_failed",
                "error": f"missing executable: {command[0]}",
                "steps": results,
            }
        started = time.monotonic()
        try:
            proc = subprocess.run(
                command,
                cwd=root,
                capture_output=True,
                text=True,
                timeout=step.timeout_s,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            return {
                "all_passed": False,
                "category": "verification_timeout",
                "error": str(exc),
                "steps": results,
            }
        output = (proc.stdout or "") + (proc.stderr or "")
        item = {
            "name": step.name,
            "phase": step.phase,
            "command": command,
            "exit_code": proc.returncode,
            "elapsed_ms": int((time.monotonic() - started) * 1000),
            "output": output[-4000:],
        }
        results.append(item)
        if proc.returncode != 0:
            category = (
                "static_failed" if step.phase == "static" else "target_tests_failed"
            )
            return {"all_passed": False, "category": category, "steps": results}
    return {"all_passed": True, "category": "target_tests_passed", "steps": results}
