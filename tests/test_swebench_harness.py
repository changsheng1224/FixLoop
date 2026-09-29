from __future__ import annotations

import json

from src.benchmark.swebench.harness import HarnessResult, _parse_report_outcome
from src.benchmark.swebench.runner import AdapterConfig, SweBenchAdapter
from src.benchmark.swebench.types import FailureClass, InstanceResult


def test_parse_official_report_preserves_error_ids(tmp_path):
    report = tmp_path / "report.json"
    report.write_text(
        json.dumps(
            {
                "resolved_ids": ["resolved-case"],
                "completed_ids": ["resolved-case", "unresolved-case"],
                "unresolved_ids": ["unresolved-case"],
                "error_ids": ["image-pull-error"],
            }
        ),
        encoding="utf-8",
    )

    assert _parse_report_outcome(report) == {
        "resolved_ids": ["resolved-case"],
        "completed_ids": ["resolved-case", "unresolved-case"],
        "unresolved_ids": ["unresolved-case"],
        "error_ids": ["image-pull-error"],
    }


def test_parse_legacy_boolean_report(tmp_path):
    report = tmp_path / "legacy.json"
    report.write_text(json.dumps({"a": True, "b": False}), encoding="utf-8")

    outcome = _parse_report_outcome(report)
    assert outcome["resolved_ids"] == ["a"]
    assert outcome["completed_ids"] == ["a", "b"]


def test_error_id_is_not_misclassified_as_unresolved(tmp_path, monkeypatch):
    config = AdapterConfig(
        output_dir=tmp_path,
        work_root=tmp_path / "work",
        instance_ids=["image-error"],
        skip_verify=True,
        allow_unverified_harness=True,
        harness_only_with_patch=False,
    )
    adapter = SweBenchAdapter(config)
    result = InstanceResult(
        instance_id="image-error",
        model_patch="diff --git a/a.py b/a.py\n",
    )
    monkeypatch.setattr(
        "src.benchmark.swebench.runner.run_official_harness",
        lambda *args, **kwargs: HarnessResult(
            ok=True,
            returncode=0,
            stdout="docker pull failed: 403 Forbidden",
            stderr="",
            error_ids=["image-error"],
            backend="wsl",
        ),
    )

    meta = adapter._apply_harness([result], tmp_path / "predictions.jsonl")

    assert meta["status"] == "completed_with_errors"
    assert meta["error_ids"] == ["image-error"]
    assert result.resolved is False
    assert result.failure_class == FailureClass.ENV
    assert result.failure_detail == "harness_instance_error"
