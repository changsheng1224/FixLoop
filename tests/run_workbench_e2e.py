"""Run only workbench acceptance groups and retain machine-readable evidence.

Usage: python tests/run_workbench_e2e.py --group api --case test_chat_sse_resume_failure_and_stop
"""

import argparse
import json
import os
import platform
import subprocess
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
GROUPS = ("api", "browser", "units", "regression", "hardening")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--group", action="append", choices=GROUPS)
    parser.add_argument(
        "--case",
        action="append",
        help="Exact pytest function/node suffix or browser case; requires one group",
    )
    parser.add_argument("--artifacts", type=Path)
    parser.add_argument("--node", default=os.environ.get("FIXLOOP_TEST_NODE", "node"))
    parser.add_argument("--playwright-module", default=os.environ.get("FIXLOOP_PLAYWRIGHT_MODULE"))
    parser.add_argument("--browser-channel", default=os.environ.get("FIXLOOP_BROWSER_CHANNEL"))
    args = parser.parse_args(argv)
    groups = args.group or ["api", "browser", "units"]
    if len(groups) != len(set(groups)):
        parser.error("duplicate group")
    if args.case and (len(groups) != 1 or groups[0] not in {"api", "browser"}):
        parser.error("--case requires exactly one --group api or --group browser")
    if args.case and any("/" in case or "\\" in case or "::" in case for case in args.case):
        parser.error(
            "--case accepts a function name with optional parameter suffix, not a file path"
        )
    destination = args.artifacts or ROOT / ".runtime-tools" / datetime.now(UTC).strftime(
        "s6-%Y%m%dT%H%M%S-%f"
    )
    destination = destination.resolve()
    if destination.exists() and any(destination.iterdir()):
        parser.error("--artifacts must name a new or empty directory; preserve earlier evidence")
    destination.mkdir(parents=True, exist_ok=True)
    environment = dict(
        os.environ,
        PYTHONUTF8="1",
        FIXLOOP_TEST_PYTHON=sys.executable,
        HF_HUB_OFFLINE="1",
        TRANSFORMERS_OFFLINE="1",
    )
    environment.pop("FIXLOOP_E2E_CASES", None)
    environment["FIXLOOP_E2E_ARTIFACTS"] = str(destination / "browser")
    if args.playwright_module:
        environment["FIXLOOP_PLAYWRIGHT_MODULE"] = args.playwright_module
    if args.browser_channel:
        environment["FIXLOOP_BROWSER_CHANNEL"] = args.browser_channel
    report = {
        "started_at": datetime.now(UTC).isoformat(),
        "platform": platform.platform(),
        "python": sys.version,
        "interpreter": sys.executable,
        "groups": [],
        "status": "running",
    }
    report_path = destination / "report.json"
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    try:
        for group in groups:
            if group == "api":
                targets = [
                    "tests/test_workbench_e2e.py" + ("::" + case if case else "")
                    for case in (args.case or [None])
                ]
                command = [
                    sys.executable,
                    "-m",
                    "pytest",
                    *targets,
                    "-q",
                    "--tb=short",
                    "-p",
                    "no:cacheprovider",
                    "--basetemp",
                    str(destination / "pytest"),
                    "--junitxml",
                    str(destination / "api.xml"),
                ]
            elif group == "browser":
                command = [args.node, "tests/workbench_e2e_browser.mjs"]
                if args.case:
                    environment["FIXLOOP_E2E_CASES"] = ",".join(args.case)
            elif group == "units":
                command = [args.node, "tests/workbench_ui_units.mjs"]
            else:
                targets = (
                    [
                        "tests/test_workbench_service.py",
                        "tests/test_workbench_project.py",
                        "tests/test_workbench_http.py",
                        "tests/test_workbench_restore.py",
                        "tests/test_workbench_ui.py",
                    ]
                    if group == "regression"
                    else [
                        "tests/test_sqlite_paths.py",
                        "tests/test_workbench_result_contract.py",
                        "tests/test_workbench_packaging.py",
                        "tests/test_workbench_transactions.py",
                        "tests/test_workbench_isolation.py",
                        "tests/test_workbench_process_scope.py",
                        "tests/test_workbench_cleanup_proof.py",
                        "tests/test_workbench_supervision.py",
                        "tests/test_workbench_context.py",
                        "tests/test_workbench_candidate_recovery.py",
                    ]
                )
                command = [
                    sys.executable,
                    "-m",
                    "pytest",
                    *targets,
                    "-q",
                    "--tb=short",
                    "-p",
                    "no:cacheprovider",
                    "--basetemp",
                    str(destination / (group + "-pytest")),
                    "--junitxml",
                    str(destination / (group + ".xml")),
                ]
            started = time.monotonic()
            item = {"name": group, "command": command, "status": "running"}
            report["groups"].append(item)
            print(f"Running {group}; evidence: {destination / (group + '.log')}", flush=True)
            with (destination / (group + ".log")).open("w", encoding="utf-8") as log:
                try:
                    process = subprocess.Popen(
                        command,
                        cwd=ROOT,
                        env=environment,
                        stdout=subprocess.PIPE,
                        stderr=subprocess.STDOUT,
                        text=True,
                        encoding="utf-8",
                        errors="replace",
                    )
                    try:
                        for line in process.stdout:
                            print(line, end="", flush=True)
                            log.write(line)
                            log.flush()
                        code = process.wait()
                    finally:
                        process.stdout.close()
                        if process.poll() is None:
                            process.terminate()
                            process.wait(10)
                except OSError as error:
                    log.write(str(error))
                    print(str(error), file=sys.stderr)
                    code = 127
            item.update(
                status="passed" if code == 0 else "failed",
                exit_code=code,
                elapsed_s=round(time.monotonic() - started, 3),
            )
            if code:
                break  # Fail closed; missing dependencies are failures, never skips.
        report["status"] = (
            "passed"
            if len(report["groups"]) == len(groups)
            and all(g["status"] == "passed" for g in report["groups"])
            else "failed"
        )
    finally:
        report["completed_at"] = datetime.now(UTC).isoformat()
        if report["status"] == "running":
            report["status"] = "interrupted"
        report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"S6 report: {report_path}", flush=True)
    return 0 if report["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
