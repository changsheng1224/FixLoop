"""Execute declarative verification steps with structured failure categories."""

from __future__ import annotations

import json
import re
import subprocess
import time
import xml.etree.ElementTree as ET
from pathlib import Path

from agent_runtime.test_execution import run_command
from src.repair.language_capabilities import project_command
from src.repair.verification.verification_profiles import VerificationProfile


def test_counts(output, language):
    """Read test summaries without treating a successful command as one test."""
    if language == "go":
        events = []
        for line in output.splitlines():
            try:
                item = json.loads(line)
                if item.get("Test") and item.get("Action") in {"pass", "fail", "skip"}:
                    events.append(item)
            except (ValueError, AttributeError):
                continue
        passed = sum(e["Action"] == "pass" for e in events)
        return len(events), passed
    summaries = re.findall(r"Tests run:\s*(\d+),\s*Failures:\s*(\d+),\s*Errors:\s*(\d+)", output)
    if summaries:
        total, failed, errors = map(int, summaries[-1])
        return total, total - failed - errors
    # Jest emits suite counts separately; only its Tests line counts test cases.
    jest = re.findall(r"^Tests:\s*(.+)$", output, re.M)
    if jest:
        text = jest[-1]
        total = re.search(r"(\d+) total", text)
        passed = re.search(r"(\d+) passed", text)
        return int(total[1]) if total else 0, int(passed[1]) if passed else 0
    node = re.search(r"# tests (\d+)", output)
    if node:
        passed = re.search(r"# pass (\d+)", output)
        return int(node[1]), int(passed[1]) if passed else 0
    counts = {
        name: sum(map(int, re.findall(rf"(\d+) {name}\b", output)))
        for name in ("passed", "failed", "skipped", "passing", "failing")
    }
    return sum(counts.values()), counts["passed"] + counts["passing"]


def _reports(root, ecosystem):
    patterns = {
        "maven": "**/target/surefire-reports/TEST-*.xml",
        "gradle": "**/build/test-results/**/TEST-*.xml",
    }
    return (
        {p: (p.stat().st_mtime_ns, p.read_bytes()) for p in root.glob(patterns[ecosystem])}
        if ecosystem in patterns
        else {}
    )


def _report_counts(reports, before):
    total = passed = 0
    for path, image in reports.items():
        if before.get(path) == image:
            continue
        suite = ET.fromstring(image[1])
        for node in [suite] if suite.tag == "testsuite" else suite.iter("testsuite"):
            count = int(node.get("tests", 0))
            total += count
            passed += count - sum(int(node.get(k, 0)) for k in ("failures", "errors", "skipped"))
    return total, passed


def run_profile(
    repo_root: str | Path,
    profile: VerificationProfile,
    *,
    target: str = "",
    run_tests: bool = True,
    cancel_token=None,
) -> dict:
    root = Path(repo_root)
    steps = list(profile.static_steps) + (list(profile.test_steps) if run_tests else [])
    results = []
    total = passed = 0
    before_reports = _reports(root, profile.ecosystem)
    for step in steps:
        command = list(step.command)
        if target and step.phase == "target_tests" and profile.language == "python":
            command.append(target)
        resolved = project_command(root, command)
        if resolved is None:
            return {
                "all_passed": False,
                "category": "verification_environment_failed",
                "error": f"missing executable: {command[0]}",
                "steps": results,
                "completed": False,
            }
        command = resolved
        started = time.monotonic()
        try:
            code, output = run_command(
                command, root, timeout_s=step.timeout_s, cancel_token=cancel_token
            )
        except subprocess.TimeoutExpired as exc:
            return {
                "all_passed": False,
                "category": "verification_timeout",
                "error": str(exc),
                "steps": results,
                "completed": False,
            }
        except OSError as exc:
            return {
                "all_passed": False,
                "category": "verification_environment_failed",
                "error": str(exc),
                "steps": results,
                "completed": False,
            }
        if step.phase == "target_tests":
            count, green = test_counts(output, profile.language)
            total += count
            passed += green
        item = {
            "name": step.name,
            "phase": step.phase,
            "command": command,
            "exit_code": code,
            "elapsed_ms": int((time.monotonic() - started) * 1000),
            "output": output[-4000:],
        }
        results.append(item)
        if code != 0:
            category = "static_failed" if step.phase == "static" else "target_tests_failed"
            return {
                "all_passed": False,
                "category": category,
                "steps": results,
                "completed": True,
                "exit_code": code,
                "total_tests": total,
                "passed": passed,
            }
    if not total:
        try:
            total, passed = _report_counts(_reports(root, profile.ecosystem), before_reports)
        except (OSError, ValueError, ET.ParseError):
            pass
    ok = bool(results) and (not run_tests or total > 0)
    return {
        "all_passed": ok,
        "category": "target_tests_passed" if ok else "no-tests",
        "steps": results,
        "completed": bool(results),
        "exit_code": 0 if results else None,
        "total_tests": total,
        "passed": passed,
    }
