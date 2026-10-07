"""Frozen R16-compatible SWE-bench Lite Dev5 execution contract."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

from src.benchmark.swebench.dev_instances import DEV_INSTANCE_IDS

ROOT = Path(__file__).resolve().parents[1]
PROTOCOL_PATH = ROOT / "configs" / "swebench" / "lite_dev5_r16_protocol.json"
SCRIPT_PATH = ROOT / "scripts" / "run_swebench_lite_dev5.ps1"


def _protocol() -> dict:
    return json.loads(PROTOCOL_PATH.read_text(encoding="utf-8"))


def test_frozen_public_input_hash_ids_and_no_answers():
    protocol = _protocol()
    instances_path = ROOT / protocol["instances_jsonl"]
    rows = [
        json.loads(line)
        for line in instances_path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]

    assert hashlib.sha256(instances_path.read_bytes()).hexdigest() == protocol["instances_sha256"]
    assert protocol["instance_ids"] == list(DEV_INSTANCE_IDS)
    assert [row["instance_id"] for row in rows] == list(DEV_INSTANCE_IDS)
    for row in rows:
        assert row["problem_statement"].strip()
        assert row["base_commit"].strip()
        assert row.get("patch", "") == ""
        assert row.get("test_patch", "") == ""
        assert json.loads(row.get("FAIL_TO_PASS", "[]")) == []
        assert json.loads(row.get("PASS_TO_PASS", "[]")) == []


def test_protocol_freezes_r16_execution_conditions():
    protocol = _protocol()

    assert protocol["provider"] == "anthropic_compat"
    assert protocol["model"] == "deepseek-v4-pro"
    assert protocol["max_retries"] == 1
    assert protocol["repair_timeout_s"] == 900
    assert protocol["max_workers"] == 1
    assert protocol["critic"] == {"enabled": True, "mode": "rules_first"}
    assert protocol["verifier"]["enabled"] is True
    assert protocol["verifier"]["require_sandbox"] is True
    assert protocol["official_harness"] is False
    assert protocol["patcher_projection"] == "public_problem_only"


def test_entrypoint_enforces_fixed_protocol_without_official_harness():
    script = SCRIPT_PATH.read_text(encoding="utf-8")

    for required in (
        "--instances-sha256",
        "--require-verifier-sandbox",
        "--instance-ids",
        "FIXLOOP_CRITIC_MODE",
        "progress.jsonl",
        "run.log",
    ):
        assert required in script
    assert '"--run-harness"' not in script
