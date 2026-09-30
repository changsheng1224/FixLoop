import json

import pytest

from src.eval.sandbox_mvp import _percentile, _request, run_isolation, run_overhead


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
