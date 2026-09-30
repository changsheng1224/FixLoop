import json
import subprocess
import uuid

import pytest

import src.eval.sandbox_mvp as evaluator
from src.eval.sandbox_mvp import _percentile, _request, run_isolation, run_overhead


class FakeBackend:
    def __init__(self, policy):
        self.policy = policy

    def preflight(self):
        return "digest"


def live_config(tmp_path, monkeypatch):
    workspace, state = tmp_path / "workspace", tmp_path / "state"
    workspace.mkdir()
    state.mkdir()
    config = tmp_path / "config.json"
    config.write_text(
        json.dumps(
            {
                "workspace": str(workspace),
                "state_root": str(state),
                "toolchain": str(tmp_path / "toolchain"),
                "helper": str(tmp_path / "helper.py"),
            }
        )
    )
    monkeypatch.setattr(evaluator, "LinuxSandboxBackend", FakeBackend)
    return config


def test_percentile_is_deterministic():
    assert _percentile([1, 2, 3, 4], 95) == 3.85


def test_isolation_records_blocked_cases(tmp_path):
    config = tmp_path / "config.json"
    config.write_text(
        json.dumps(
            {
                "workspace": str(tmp_path / "workspace"),
                "state_root": str(tmp_path / "state"),
                "toolchain": str(tmp_path / "toolchain"),
                "helper": str(tmp_path / "helper.py"),
            }
        )
    )
    report, code = run_isolation(str(config), tmp_path / "result")
    assert code == 2
    assert report["complete"] is False
    assert report["counts"]["blocked"] == 13
    lines = (tmp_path / "result" / "cases.jsonl").read_text().splitlines()
    assert len(lines) == 13
    assert all(json.loads(line)["outcome"] == "blocked" for line in lines)
    manifest = json.loads((tmp_path / "result" / "environment_manifest.json").read_text())
    assert manifest["preflight_ms"] >= 0
    assert manifest["policy_digest"] == ""
    assert not (tmp_path / "result" / "policy.json").exists()


def test_fixed_request_uses_sandbox_toolchain():
    request = _request("run", "call", "print('ok')")
    assert request.argv == ("/toolchain/bin/python", "-I", "-c", "print('ok')")


def test_overhead_rejects_short_sample(tmp_path):
    with pytest.raises(ValueError, match="at least 10"):
        run_overhead(str(tmp_path / "missing.json"), tmp_path / "output", 9)
    assert not (tmp_path / "output").exists()


def test_overhead_records_host_timeouts_without_losing_other_samples(tmp_path, monkeypatch):
    config = live_config(tmp_path, monkeypatch)

    def sandbox_sample(*_args, **_kwargs):
        receipt_id = "call-" + uuid.uuid4().hex
        return (
            {
                "result": {
                    "execution_status": "completed",
                    "exit_code": 0,
                    "cleanup": "confirmed",
                    "startup_ms": 1,
                    "duration_ms": 3,
                    "cleanup_ms": 1,
                    "stdout_excerpt": "",
                    "stderr_excerpt": "",
                    "output_truncated": False,
                    "error_code": "",
                },
                "receipt_id": receipt_id,
            },
            {"call_id": receipt_id},
        )

    monkeypatch.setattr(evaluator, "_case", sandbox_sample)

    def timeout(*args, **kwargs):
        raise subprocess.TimeoutExpired(args[0], kwargs["timeout"])

    monkeypatch.setattr(evaluator.subprocess, "run", timeout)
    report, status = run_overhead(str(config), tmp_path / "results", 10)
    rows = [
        json.loads(line)
        for line in (tmp_path / "results" / "overhead.jsonl").read_text().splitlines()
    ]
    assert status == 1
    assert report["counts"] == {"passed": 44, "failed": 44, "pending": 0, "blocked": 0}
    assert len(rows) == 88
    assert all(
        row["error"] == "host_timeout" for row in rows if row["tier"] == "trusted_host_linux"
    )
    assert report["statistics"]["startup"]["trusted_host_linux"] == {
        "successful_samples": 0,
        "median_ms": None,
        "p95_ms": None,
    }


def test_isolation_records_handler_failure_and_continues(tmp_path, monkeypatch):
    config = live_config(tmp_path, monkeypatch)

    def fail(*args):
        raise OSError("fixture failed")

    def pass_case(_config, _run_id, case_id, _receipts):
        return {"case_id": case_id, "outcome": "passed"}

    monkeypatch.setattr(
        evaluator,
        "_isolation_scenarios",
        lambda: {case: fail if case == "S2" else pass_case for case in evaluator.CASES},
    )
    report, status = run_isolation(str(config), tmp_path / "results")
    rows = [
        json.loads(line) for line in (tmp_path / "results" / "cases.jsonl").read_text().splitlines()
    ]
    assert status == 1
    assert report["counts"]["failed"] == 1
    assert len(rows) == 13
    assert rows[1]["reason"] == "fixture failed"


def test_overhead_stops_sandbox_calls_after_missing_receipt(tmp_path, monkeypatch):
    config = live_config(tmp_path, monkeypatch)
    calls = []

    def missing_receipt(*_args, **_kwargs):
        calls.append(True)
        return (
            {
                "result": {
                    "execution_status": "uncertain",
                    "exit_code": None,
                    "cleanup": "unverified",
                    "startup_ms": 1,
                    "duration_ms": 2,
                    "cleanup_ms": 1,
                    "stdout_excerpt": "",
                    "stderr_excerpt": "",
                    "output_truncated": False,
                    "error_code": "execution_uncertain",
                },
                "reason": "receipt missing",
            },
            None,
        )

    monkeypatch.setattr(evaluator, "_case", missing_receipt)
    monkeypatch.setattr(
        evaluator.subprocess,
        "run",
        lambda *args, **kwargs: subprocess.CompletedProcess(args[0], 0, b"", b""),
    )
    report, status = run_overhead(str(config), tmp_path / "results", 10)
    rows = [
        json.loads(line)
        for line in (tmp_path / "results" / "overhead.jsonl").read_text().splitlines()
    ]
    assert status == 1
    assert len(calls) == 1
    assert report["counts"] == {"passed": 44, "failed": 1, "pending": 0, "blocked": 43}
    assert len(rows) == 88
    assert rows[-1]["error"] == "previous_sandbox_sample_uncertain"
