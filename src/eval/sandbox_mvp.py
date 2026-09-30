"""Fixed, non-production WSL/bwrap isolation and overhead evaluation."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import shutil
import signal
import socket
import statistics
import subprocess
import sys
import threading
import time
import uuid
from dataclasses import asdict
from pathlib import Path

from agent_runtime.linux_sandbox import LinuxSandboxBackend, SandboxPolicy, SandboxRequest
from agent_runtime.linux_sandbox.receipts import process_identity, receipt_checksum

FIXTURES = Path(__file__).resolve().parents[2] / "tests/fixtures/linux_sandbox"
CASES = tuple(f"S{i}" for i in range(1, 14))
TASKS = ("startup", "write", "pytest", "output")


def _write_json(path: Path, data: dict) -> None:
    path.write_text(json.dumps(data, indent=2, sort_keys=True), encoding="utf-8")


def _hash_fixtures() -> str:
    digest = hashlib.sha256()
    for file in sorted(FIXTURES.glob("*.py")):
        digest.update(file.name.encode() + file.read_bytes())
    return digest.hexdigest()


def _config(path: Path) -> dict:
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict) or set(data) != {
        "workspace",
        "state_root",
        "toolchain",
        "helper",
    }:
        raise ValueError("policy_denied: fixed evaluator config required")
    return data


def _policy(config: dict, workspace: Path, state: Path) -> SandboxPolicy:
    return SandboxPolicy(workspace, state, Path(config["toolchain"]), Path(config["helper"]))


def _request(
    run_id: str,
    call_id: str,
    code: str,
    *,
    timeout: float = 10,
    limit: int = 1048576,
    operation: str = "command",
) -> SandboxRequest:
    if operation == "pytest":
        argv = (
            "/toolchain/bin/python",
            "-I",
            "-m",
            "pytest",
            "-q",
            "-p",
            "no:cacheprovider",
            "test_fixture.py",
        )
    else:
        argv = ("/toolchain/bin/python", "-I", "-c", code)
    return SandboxRequest(
        "p4", "p4", run_id, call_id, operation, argv, timeout_s=timeout, output_limit_bytes=limit
    )


def _workspace(config: dict, name: str) -> tuple[Path, Path]:
    base = Path(config["workspace"]).resolve()
    state_base = Path(config["state_root"]).resolve()
    if not base.is_dir() or not state_base.is_dir() or base == state_base:
        raise ValueError("workspace_mapping_rejected: native fixture roots required")
    workspace, state = base / name, state_base / name
    workspace.mkdir(mode=0o700)
    state.mkdir(mode=0o700)
    shutil.copyfile(FIXTURES / "test_fixture.py", workspace / "test_fixture.py")
    return workspace, state


def _case(
    backend: LinuxSandboxBackend,
    run_id: str,
    case_id: str,
    code: str,
    *,
    timeout: float = 10,
    limit: int = 1048576,
    operation: str = "command",
) -> tuple[dict, dict | None]:
    started = time.perf_counter()
    request = _request(
        run_id,
        "call-" + uuid.uuid4().hex[:20],
        code,
        timeout=timeout,
        limit=limit,
        operation=operation,
    )
    result = backend.execute(request)
    elapsed = round((time.perf_counter() - started) * 1000, 3)
    receipt = None
    try:
        receipt = backend.store.reconcile(backend.policy.digest())
        if receipt["call_id"] != request.call_id or receipt["result"] != result.to_wire():
            raise ValueError("receipt_invalid: result mismatch")
    except (OSError, TypeError, ValueError) as exc:
        return {
            "case_id": case_id,
            "outcome": "failed",
            "reason": str(exc),
            "elapsed_ms": elapsed,
            "result": result.to_wire(),
        }, None
    return {
        "case_id": case_id,
        "outcome": "pending",
        "elapsed_ms": elapsed,
        "result": result.to_wire(),
        "call_id": request.call_id,
        "receipt_id": request.call_id,
        "receipt_checksum": receipt_checksum(receipt),
    }, receipt


def _assess(row: dict, condition: bool, reason: str) -> dict:
    row["outcome"] = "passed" if condition else "failed"
    if not condition:
        row["reason"] = reason
    return row


def _percentile(values: list[float], percent: float) -> float:
    values = sorted(values)
    if not values:
        return 0.0
    index = (len(values) - 1) * percent / 100
    lower = int(index)
    fraction = index - lower
    return values[lower] + (values[min(lower + 1, len(values) - 1)] - values[lower]) * fraction


def _write_rows(path: Path, rows: list[dict]) -> None:
    with path.open("w", encoding="utf-8") as stream:
        for row in rows:
            stream.write(json.dumps(row, sort_keys=True) + "\n")


def _environment(run_id: str, digest: str, toolchain: Path | None = None) -> dict:
    details = {
        "run_id": run_id,
        "platform": platform.platform(),
        "python": sys.version,
        "fixture_sha256": _hash_fixtures(),
        "evaluator_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        "policy_digest": digest,
    }
    if sys.platform == "linux":
        details["kernel"] = platform.release()
        details["distribution"] = Path("/etc/os-release").read_text(encoding="utf-8")
        for name, argv in {
            "bwrap": ["/usr/bin/bwrap", "--version"],
            "toolchain_python": [str(toolchain / "bin/python"), "--version"]
            if toolchain is not None
            else ["/usr/bin/python3.14", "--version"],
        }.items():
            try:
                version = subprocess.run(
                    argv, capture_output=True, text=True, timeout=3, check=False
                )
                details[name] = (version.stdout or version.stderr).strip()
            except (OSError, subprocess.TimeoutExpired) as exc:
                details[name] = f"unavailable: {exc}"
    return details


def _report(out: Path, suite: str, rows: list[dict], blocked: str = "") -> tuple[dict, int]:
    counts = {
        key: sum(r.get("outcome") == key for r in rows)
        for key in ("passed", "failed", "pending", "blocked")
    }
    summary = {
        "suite": suite,
        "counts": counts,
        "blocked_reason": blocked,
        "complete": not blocked
        and not (counts["failed"] or counts["pending"] or counts["blocked"]),
    }
    (out / "report.md").write_text(
        "# WSL sandbox " + suite + "\n\n```json\n" + json.dumps(summary, indent=2) + "\n```\n",
        encoding="utf-8",
    )
    return summary, 2 if blocked else (0 if summary["complete"] else 1)


def _wait_for(path: Path, timeout: float = 4) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if path.exists():
            return True
        time.sleep(0.03)
    return False


def _live_case(config: dict, run_id: str, case_id: str, receipts: Path) -> dict:
    workspace, state = _workspace(config, run_id + "-" + case_id.lower())
    backend = LinuxSandboxBackend(_policy(config, workspace, state))
    request = _request(
        run_id,
        "call-" + uuid.uuid4().hex[:20],
        "from pathlib import Path; import time; "
        "Path('marker').write_text('started'); time.sleep(5)",
    )
    outcome: list = []
    worker = threading.Thread(target=lambda: outcome.append(backend.execute(request)))
    worker.start()
    row = {"case_id": case_id, "outcome": "failed", "reason": "worker did not finish"}
    try:
        if not _wait_for(workspace / "marker"):
            row["reason"] = "target never started"
            return row
        current = backend.store.current()
        if case_id == "S7":
            with backend._guard:
                proc = backend._active.get(request.call_id)
            identity = current.get("supervisor_identity") if current else None
            if proc is None or identity is None or process_identity(proc.pid) != identity:
                row["reason"] = "supervisor identity could not be verified"
                return row
            os.kill(proc.pid, signal.SIGKILL)
            worker.join(timeout=8)
            blocked = backend.execute(_request(run_id, "after-crash", "print('unexpected')"))
            passed = (
                bool(outcome)
                and outcome[0].execution_status == "uncertain"
                and blocked.execution_status == "rejected"
                and blocked.error_code == "execution_uncertain"
                and backend.store.current()["state"] == "running"
            )
            row.update(
                {
                    "outcome": "passed" if passed else "failed",
                    "reason": "" if passed else "supervisor death was not fail-closed",
                    "result": outcome[0].to_wire() if outcome else {},
                    "next_result": blocked.to_wire(),
                    "supervisor_identity": identity,
                }
            )
        else:
            busy = backend.execute(_request(run_id, "competing", "print('unexpected')"))
            cancelled = backend.cancel(request.call_id)
            worker.join(timeout=8)
            try:
                receipt = backend.reconcile()
            except ValueError:
                receipt = None
            if receipt:
                _write_json(receipts / "S12.json", receipt)
            passed = (
                busy.error_code == "workspace_busy"
                and cancelled
                and bool(outcome)
                and outcome[0].execution_status == "cancelled"
                and outcome[0].cleanup == "confirmed"
                and receipt is not None
            )
            row.update(
                {
                    "outcome": "passed" if passed else "failed",
                    "reason": "" if passed else "competition or cancellation failed",
                    "busy_result": busy.to_wire(),
                    "result": outcome[0].to_wire() if outcome else {},
                }
            )
        return row
    finally:
        if worker.is_alive():
            backend.cancel(request.call_id)
            worker.join(timeout=8)


def _cancel_detached(config: dict, run_id: str, receipts: Path) -> bool:
    workspace, state = _workspace(config, run_id + "-s6-cancel")
    backend = LinuxSandboxBackend(_policy(config, workspace, state))
    code = (
        "import os,time,pathlib\n"
        "p=pathlib.Path('/workspace/heartbeat')\n"
        "if os.fork()==0:\n"
        " os.setsid()\n"
        " if os.fork()==0:\n"
        "  while True:\n"
        "   p.write_text(str(time.monotonic()))\n"
        "   time.sleep(.05)\n"
        " os._exit(0)\n"
        "else:\n"
        " time.sleep(5)\n"
    )
    request = _request(run_id, "cancel-" + uuid.uuid4().hex[:20], code)
    outcome: list = []
    worker = threading.Thread(target=lambda: outcome.append(backend.execute(request)))
    worker.start()
    try:
        heartbeat = workspace / "heartbeat"
        if not _wait_for(heartbeat):
            return False
        if not backend.cancel(request.call_id):
            return False
        worker.join(timeout=8)
        receipt = backend.reconcile()
        if receipt:
            _write_json(receipts / "S6-cancel.json", receipt)
        before = heartbeat.read_text()
        time.sleep(0.3)
        return (
            bool(outcome)
            and outcome[0].execution_status == "cancelled"
            and outcome[0].cleanup == "confirmed"
            and receipt is not None
            and heartbeat.read_text() == before
        )
    finally:
        if worker.is_alive():
            backend.cancel(request.call_id)
            worker.join(timeout=8)


def _controller_death(config: dict, run_id: str, receipts: Path) -> dict:
    workspace, state = _workspace(config, run_id + "-s10-crash")
    backend = LinuxSandboxBackend(_policy(config, workspace, state))
    request = _request(
        run_id,
        "crash-" + uuid.uuid4().hex[:20],
        "from pathlib import Path; import time; "
        "Path('partial').write_text('changed'); time.sleep(5)",
    )
    child = os.fork()
    if child == 0:
        backend.execute(request)
        os._exit(0)
    try:
        if not _wait_for(workspace / "partial"):
            return {"case_id": "S10", "outcome": "failed", "reason": "partial write missing"}
        os.kill(child, signal.SIGKILL)
        os.waitpid(child, 0)
        child = 0
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            current = backend.store.current()
            if current and current["state"] == "terminal":
                break
            time.sleep(0.05)
        try:
            receipt = backend.reconcile()
        except ValueError as exc:
            return {"case_id": "S10", "outcome": "failed", "reason": str(exc)}
        if receipt:
            _write_json(receipts / "S10.json", receipt)
        before = (workspace / "partial").read_text()
        replay = backend.execute(request)
        passed = (
            before == "changed"
            and receipt is not None
            and receipt["result"]["cleanup"] == "confirmed"
            and replay.execution_status == "rejected"
            and (workspace / "partial").read_text() == before
        )
        return {
            "case_id": "S10",
            "outcome": "passed" if passed else "failed",
            "receipt_id": request.call_id,
            "call_id": request.call_id,
            "policy_digest": receipt["policy_digest"] if receipt else "",
            "replay": replay.to_wire(),
            "reason": "" if passed else "crash cleanup or replay check failed",
        }
    finally:
        if child:
            try:
                os.kill(child, signal.SIGKILL)
            except ProcessLookupError:
                pass
            os.waitpid(child, 0)


def run_isolation(config_path: str, output_dir: Path) -> tuple[dict, int]:
    output_dir.mkdir(parents=True, exist_ok=True)
    receipts = output_dir / "receipts"
    receipts.mkdir(exist_ok=True)
    rows: list[dict] = []
    run_id = "p4-" + uuid.uuid4().hex[:16]
    config = {}
    blocked = ""
    preflight_started = time.perf_counter()
    try:
        config = _config(Path(config_path))
        workspace, state = _workspace(config, run_id + "-preflight")
        policy = _policy(config, workspace, state)
        digest = LinuxSandboxBackend(policy).preflight()
    except (OSError, ValueError, KeyError) as exc:
        blocked = str(exc)
        digest = ""
    manifest = _environment(run_id, digest, Path(config["toolchain"]) if config else None)
    manifest.update(
        {
            "workspace_base": config.get("workspace", ""),
            "blocked_reason": blocked,
            "preflight_ms": round((time.perf_counter() - preflight_started) * 1000, 3),
        }
    )
    _write_json(output_dir / "environment_manifest.json", manifest)
    if blocked:
        rows = [{"case_id": case, "outcome": "blocked", "reason": blocked} for case in CASES]
    else:
        _write_json(
            output_dir / "policy.json", {key: str(value) for key, value in asdict(policy).items()}
        )
        specs = {
            "S1": (
                "from pathlib import Path; Path('written').write_text('ok'); print('ok')",
                "write",
            ),
            "S2": (
                "from pathlib import Path; print(Path('escape').exists(), "
                "Path('/workspace/../state/marker').exists())",
                "hidden",
            ),
            "S3": (
                "from pathlib import Path; "
                "print(Path('/home/haoyu/.ssh').exists(), "
                "Path('/run/docker.sock').exists())",
                "hidden",
            ),
            "S5": (
                "from pathlib import Path; print("
                "Path('/mnt/c/Windows/System32/cmd.exe').exists(), "
                "Path('/run/WSL').exists(), "
                "Path('/proc/sys/fs/binfmt_misc/WSLInterop').exists())",
                "hidden",
            ),
            "S6": ("import time; time.sleep(3)", "timeout"),
            "S9": ("import sys; sys.stdout.write('x'*100000)", "output"),
            "S10": (
                "from pathlib import Path; import time; "
                "Path('partial').write_text('changed'); time.sleep(3)",
                "partial",
            ),
        }
        for case_id in CASES:
            if case_id in {"S7", "S12"}:
                rows.append(_live_case(config, run_id, case_id, receipts))
                continue
            if case_id == "S10":
                rows.append(_controller_death(config, run_id, receipts))
                continue
            if case_id == "S8":
                workspace, state = _workspace(config, run_id + "-s8")
                bad = SandboxPolicy(workspace, state, state / "missing", Path(config["helper"]))
                result = LinuxSandboxBackend(bad).execute(
                    _request(run_id, "missing-toolchain", "print('must not run')")
                )
                rows.append(
                    {
                        "case_id": case_id,
                        "outcome": "passed"
                        if (
                            result.execution_status == "rejected"
                            and result.error_code
                            in {"toolchain_unavailable", "workspace_mapping_rejected"}
                            and not LinuxSandboxBackend(bad).store.registry.exists()
                        )
                        else "failed",
                        "result": result.to_wire(),
                    }
                )
                continue
            if case_id == "S4":
                with socket.socket() as listener:
                    listener.bind(("127.0.0.1", 0))
                    listener.listen(1)
                    port = listener.getsockname()[1]
                    with socket.create_connection(("127.0.0.1", port), timeout=1):
                        pass  # Positive control in the trusted WSL host.
                    workspace, state = _workspace(config, run_id + "-s4")
                    backend = LinuxSandboxBackend(_policy(config, workspace, state))
                    probe = (
                        "import socket; s=socket.socket(); s.settimeout(1); "
                        f"print(s.connect_ex(('127.0.0.1',{port})) != 0)"
                    )
                    row, receipt = _case(backend, run_id, case_id, probe)
                if receipt is not None:
                    _write_json(receipts / "S4.json", receipt)
                    result = row["result"]
                    _assess(
                        row,
                        result["execution_status"] == "completed"
                        and result["exit_code"] == 0
                        and result["cleanup"] == "confirmed"
                        and result["stdout_excerpt"].strip() == "True",
                        "numeric loopback connection was not isolated",
                    )
                rows.append(row)
                continue
            if case_id == "S11":
                workspace, state = _workspace(config, run_id + "-s11")
                backend = LinuxSandboxBackend(_policy(config, workspace, state))
                row, receipt = _case(backend, run_id, case_id, "print('ok')")
                if receipt is not None:
                    _write_json(receipts / "S11-original.json", receipt)
                    path = backend.store.receipt_path(row["receipt_id"])
                    corrupted = json.loads(path.read_text(encoding="utf-8"))
                    corrupted["sha256"] = "0" * 64
                    path.write_text(json.dumps(corrupted), encoding="utf-8")
                    try:
                        backend.reconcile()
                    except ValueError as exc:
                        _assess(
                            row, str(exc).startswith("receipt_invalid"), "corrupt receipt accepted"
                        )
                    else:
                        _assess(row, False, "corrupt receipt accepted")
                rows.append(row)
                continue
            if case_id not in specs:
                rows.append(
                    {
                        "case_id": case_id,
                        "outcome": "pending",
                        "reason": "S13 requires an authorized real-model repair run",
                    }
                )
                continue
            code, kind = specs[case_id]
            workspace, state = _workspace(config, run_id + "-" + case_id.lower())
            if kind == "hidden":
                (state / "marker").write_text("control-state-sentinel")
            if case_id == "S2":
                (workspace / "escape").symlink_to(state / "marker")
            if case_id == "S3":
                code = (
                    "from pathlib import Path; "
                    f"print(Path({str(state / 'marker')!r}).exists(), "
                    "Path('/home/haoyu/.ssh').exists(), "
                    "Path('/run/docker.sock').exists())"
                )
            if case_id == "S6":
                code = (
                    "import os,time,pathlib\n"
                    "p=pathlib.Path('/workspace/heartbeat')\n"
                    "if os.fork()==0:\n"
                    " os.setsid()\n"
                    " while True:\n"
                    "  p.write_text(str(time.monotonic()))\n"
                    "  time.sleep(.05)\n"
                    "else:\n"
                    " time.sleep(3)\n"
                )
            backend = LinuxSandboxBackend(_policy(config, workspace, state))
            row, receipt = _case(
                backend,
                run_id,
                case_id,
                code,
                timeout=1 if kind in {"timeout", "partial"} else 10,
                limit=1024 if kind == "output" else 1048576,
            )
            if receipt is not None:
                _write_json(receipts / (case_id + ".json"), receipt)
                result = row["result"]
                if kind in {"write", "hidden"}:
                    expected = (
                        "ok"
                        if kind == "write"
                        else "False False False"
                        if case_id in {"S3", "S5"}
                        else "False False"
                    )
                    condition = (
                        result["execution_status"] == "completed"
                        and result["exit_code"] == 0
                        and result["cleanup"] == "confirmed"
                        and result["stdout_excerpt"].strip() == expected
                    )
                    if kind == "write":
                        condition &= (workspace / "written").read_text() == "ok"
                        test_row, test_receipt = _case(
                            backend, run_id, "S1-pytest", "", operation="pytest"
                        )
                        if test_receipt is not None:
                            _write_json(receipts / "S1-pytest.json", test_receipt)
                        test_result = test_row["result"]
                        condition &= (
                            test_receipt is not None
                            and test_result["exit_code"] == 0
                            and test_result["cleanup"] == "confirmed"
                            and "1 passed" in test_result["stdout_excerpt"]
                        )
                        readonly, readonly_receipt = _case(
                            backend,
                            run_id,
                            "S1-readonly",
                            "open('/toolchain/forbidden','wb').write(b'x')",
                        )
                        if readonly_receipt is not None:
                            _write_json(receipts / "S1-readonly.json", readonly_receipt)
                        condition &= (
                            readonly_receipt is not None
                            and readonly["result"]["exit_code"] != 0
                            and "Errno 30" in readonly["result"]["stderr_excerpt"]
                        )
                elif kind == "output":
                    condition = (
                        result["execution_status"] == "output_limit_exceeded"
                        and result["cleanup"] == "confirmed"
                    )
                    tmp, tmp_receipt = _case(
                        backend,
                        run_id,
                        "S9-tmpfs",
                        "with open('/tmp/full','wb') as f:\n f.write(b'x'*67108865)",
                    )
                    if tmp_receipt is not None:
                        _write_json(receipts / "S9-tmpfs.json", tmp_receipt)
                    condition &= (
                        tmp_receipt is not None
                        and tmp["result"]["exit_code"] != 0
                        and "Errno 28" in tmp["result"]["stderr_excerpt"]
                        and tmp["result"]["cleanup"] == "confirmed"
                    )
                else:
                    condition = (
                        result["execution_status"] == "timeout" and result["cleanup"] == "confirmed"
                    )
                    if kind == "partial":
                        condition &= (workspace / "partial").read_text() == "changed"
                        try:
                            backend.reconcile()
                        except ValueError:
                            condition = False
                    if case_id == "S6":
                        heartbeat = workspace / "heartbeat"
                        if heartbeat.exists():
                            before = heartbeat.read_text()
                            time.sleep(0.3)
                            condition &= heartbeat.read_text() == before
                        else:
                            condition = False
                _assess(row, condition, "unexpected result or cleanup")
                if case_id == "S6" and row["outcome"] == "passed":
                    _assess(
                        row,
                        _cancel_detached(config, run_id, receipts),
                        "explicit cancellation of detached child failed",
                    )
            rows.append(row)
    for row in rows:
        row.update(
            {
                "task_id": "p4",
                "run_id": run_id,
                "fixture_sha256": manifest["fixture_sha256"],
                "evaluator_sha256": manifest["evaluator_sha256"],
                "policy_digest": row.get("policy_digest")
                or row.get("result", {}).get("policy_digest", ""),
                "expected": "scripted scenario verified",
                "actual": row["outcome"],
            }
        )
    _write_rows(output_dir / "cases.jsonl", rows)
    trace_dir = output_dir / "traces"
    trace_dir.mkdir(exist_ok=True)
    _write_rows(
        trace_dir / "evaluation.jsonl",
        [
            {
                "event": "sandbox_eval_case",
                "run_id": run_id,
                "case_id": row["case_id"],
                "outcome": row["outcome"],
                "receipt_id": row.get("receipt_id", ""),
            }
            for row in rows
        ],
    )
    return _report(output_dir, "isolation", rows, blocked)


def run_overhead(config_path: str, output_dir: Path, repetitions: int) -> tuple[dict, int]:
    if repetitions < 10:
        raise ValueError("at least 10 repetitions required")
    output_dir.mkdir(parents=True, exist_ok=True)
    receipts = output_dir / "receipts"
    receipts.mkdir(exist_ok=True)
    run_id = "p4-" + uuid.uuid4().hex[:16]
    preflight_started = time.perf_counter()
    try:
        config = _config(Path(config_path))
        workspace, state = _workspace(config, run_id + "-overhead")
        backend = LinuxSandboxBackend(_policy(config, workspace, state))
        digest = backend.preflight()
    except (OSError, ValueError, KeyError) as exc:
        _write_json(output_dir / "environment_manifest.json", _environment(run_id, ""))
        _write_rows(output_dir / "overhead.jsonl", [])
        return _report(output_dir, "overhead", [], str(exc))
    preflight_ms = round((time.perf_counter() - preflight_started) * 1000, 3)
    manifest = _environment(run_id, digest, backend.policy.toolchain)
    manifest.update(
        {
            "toolchain": str(backend.policy.toolchain),
            "repetitions": repetitions,
            "preflight_ms": preflight_ms,
        }
    )
    _write_json(output_dir / "environment_manifest.json", manifest)
    _write_json(
        output_dir / "policy.json",
        {key: str(value) for key, value in asdict(backend.policy).items()},
    )
    rows = []
    code = {
        "startup": "pass",
        "write": "from pathlib import Path; Path('written').write_text('ok')",
        "pytest": "",
        "output": "print('x'*4096)",
    }
    for task in TASKS:
        for tier in ("trusted_host_linux", "wsl_bwrap"):
            for index in range(repetitions + 1):
                started = time.perf_counter()
                if tier == "wsl_bwrap":
                    row, receipt = _case(
                        backend,
                        run_id,
                        task,
                        code[task],
                        operation="pytest" if task == "pytest" else "command",
                    )
                    result = row["result"]
                    ok = (
                        receipt is not None
                        and result["execution_status"] == "completed"
                        and result["exit_code"] == 0
                        and result["cleanup"] == "confirmed"
                    )
                    if receipt is not None:
                        _write_json(receipts / (row["receipt_id"] + ".json"), receipt)
                    timings = {
                        key: result[key] for key in ("startup_ms", "duration_ms", "cleanup_ms")
                    }
                    timings["execute_ms"] = max(
                        0, result["duration_ms"] - result["startup_ms"] - result["cleanup_ms"]
                    )
                    receipt_id = row.get("receipt_id", "")
                    error = result["error_code"] or row.get("reason", "")
                    output_bytes = len(result["stdout_excerpt"].encode()) + len(
                        result["stderr_excerpt"].encode()
                    )
                    exit_code = result["exit_code"]
                    execution_status = result["execution_status"]
                    cleanup = result["cleanup"]
                    output_truncated = result["output_truncated"]
                else:
                    argv = [str(backend.policy.toolchain / "bin/python"), "-I"]
                    argv += (
                        ["-m", "pytest", "-q", "-p", "no:cacheprovider", "test_fixture.py"]
                        if task == "pytest"
                        else ["-c", code[task]]
                    )
                    completed = subprocess.run(
                        argv,
                        cwd=workspace,
                        env={
                            "PATH": str(backend.policy.toolchain / "bin"),
                            "HOME": str(state),
                            "PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1",
                        },
                        capture_output=True,
                        timeout=15,
                        check=False,
                    )
                    ok = completed.returncode == 0
                    timings, receipt_id = {}, ""
                    error = completed.stderr.decode(errors="replace")[:200] if not ok else ""
                    output_bytes = len(completed.stdout) + len(completed.stderr)
                    exit_code = completed.returncode
                    execution_status = "completed" if ok else "failed"
                    cleanup = "not_applicable"
                    output_truncated = False
                rows.append(
                    {
                        "run_id": run_id,
                        "task": task,
                        "tier": tier,
                        "index": index,
                        "warmup": index == 0,
                        "total_ms": round((time.perf_counter() - started) * 1000, 3),
                        "timings": timings,
                        "output_bytes": output_bytes,
                        "output_truncated": output_truncated,
                        "exit_code": exit_code,
                        "execution_status": execution_status,
                        "cleanup": cleanup,
                        "policy_digest": digest,
                        "receipt_id": receipt_id,
                        "outcome": "passed" if ok else "failed",
                        "error": error,
                    }
                )
    _write_rows(output_dir / "overhead.jsonl", rows)
    summary, code_status = _report(output_dir, "overhead", rows)
    summary["statistics"] = {
        task: {
            tier: {
                "median_ms": round(statistics.median(values), 3),
                "p95_ms": round(_percentile(values, 95), 3),
            }
            for tier in ("trusted_host_linux", "wsl_bwrap")
            for values in [
                [
                    r["total_ms"]
                    for r in rows
                    if r["task"] == task and r["tier"] == tier and not r["warmup"]
                ]
            ]
        }
        for task in TASKS
    }
    _write_json(output_dir / "summary.json", summary)
    (output_dir / "report.md").write_text(
        "# WSL sandbox overhead\n\n```json\n" + json.dumps(summary, indent=2) + "\n```\n",
        encoding="utf-8",
    )
    return summary, code_status


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m src.eval.sandbox_mvp")
    parser.add_argument("--config", required=True)
    parser.add_argument("--suite", choices=("isolation", "overhead"), required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--repetitions", type=int, default=10)
    args = parser.parse_args(argv)
    try:
        report, status = (
            run_isolation(args.config, Path(args.output))
            if args.suite == "isolation"
            else run_overhead(args.config, Path(args.output), args.repetitions)
        )
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        parser.exit(2, f"blocked: {exc}\n")
    print(json.dumps(report, sort_keys=True))
    return status


if __name__ == "__main__":
    raise SystemExit(main())
