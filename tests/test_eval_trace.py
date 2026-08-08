"""Evaluation-level Trace lineage tests."""

import json
from pathlib import Path

from agent_runtime.canonical_trace import validate_trace
from agent_runtime.run_store import read_trace_path
from src.eval.contracts import (
    EvaluationContract,
    FailureClass,
    FailureCode,
    attribute_failure,
)
from src.eval.fake_runner import fake_orchestrator_factory
from src.eval.metrics import case_result_from_dict
from src.eval.runner import EvalRunner

CASES_DIR = Path(__file__).resolve().parents[1] / "src" / "eval" / "cases"


def test_evaluation_contract_attributes_failures_deterministically():
    contract = EvaluationContract(
        baseline_failed=True,
        target_passed=False,
        regression_passed=True,
        environment_ok=True,
        patch_present=True,
    )

    failure_class, failure_code = attribute_failure(contract)

    assert failure_class == FailureClass.TARGET_TEST
    assert failure_code == FailureCode.TARGET_TEST_FAILED
    assert contract.validate() == [FailureCode.TARGET_TEST_FAILED.value]


def test_eval_run_emits_canonical_lifecycle_trace(tmp_path):
    runner = EvalRunner(
        orchestrator_factory=fake_orchestrator_factory(CASES_DIR),
        cases_dir=CASES_DIR,
        output_dir=tmp_path,
    )

    report = runner.run_all(["case_001"])

    assert report.eval_run_id.startswith("eval-")
    trace_path = Path(report.trace_path)
    assert trace_path.is_file()
    events = [json.loads(line) for line in read_trace_path(trace_path)]
    assert not validate_trace(events)
    assert [event["event"] for event in events] == [
        "evaluation_started",
        "evaluation_contract_checked",
        "evaluation_finished",
    ]
    assert report.cases[0].eval_run_id == report.eval_run_id
    assert events[1]["payload"]["case_run_id"] == report.cases[0].run_id


def test_case_result_roundtrip_preserves_lineage():
    source = {
        "case_id": "case_001",
        "run_id": "repair-1",
        "trace_path": "runs/case_001/repair-1/trace.jsonl",
        "eval_run_id": "eval-1",
        "manifest_fingerprint": "manifest-1",
        "failure_class": "none",
        "failure_code": "none",
        "replay": {"available": True, "valid": True},
    }

    result = case_result_from_dict(source)

    assert result.run_id == "repair-1"
    assert result.eval_run_id == "eval-1"
    assert result.trace_path.endswith("trace.jsonl")
    assert result.manifest_fingerprint == "manifest-1"
    assert result.replay["valid"] is True
