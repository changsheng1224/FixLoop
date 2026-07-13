"""Pass@k 指标单测（V1.4-Bonus5b）。"""

from __future__ import annotations

import tempfile
from pathlib import Path

import pytest

from src.eval.metrics import compute_pass_at_k
from src.eval.models import CaseResult
from src.eval.runner import DEFAULT_CASES_DIR, EvalRunner


def _fake_factory(repo_path: str):
    from src.eval.fake_runner import FakePatchOrchestrator
    return FakePatchOrchestrator(repo_path, cases_dir=DEFAULT_CASES_DIR)


# ---------------------------------------------------------------------------
# compute_pass_at_k
# ---------------------------------------------------------------------------


class TestComputePassAtK:
    def test_empty_results(self):
        assert compute_pass_at_k([]) == {"pass@1": 0.0, "pass@3": 0.0}

    def test_all_pass(self):
        results = [
            CaseResult(case_id="case_001", fixed=True, run_index=0),
            CaseResult(case_id="case_002", fixed=True, run_index=0),
            CaseResult(case_id="case_003", fixed=True, run_index=0),
        ]
        pk = compute_pass_at_k(results)
        assert pk["pass@1"] == 1.0
        assert pk["pass@3"] == 1.0

    def test_all_fail(self):
        results = [
            CaseResult(case_id="case_001", fixed=False, run_index=0),
            CaseResult(case_id="case_002", fixed=False, run_index=0),
        ]
        pk = compute_pass_at_k(results)
        assert pk["pass@1"] == 0.0
        assert pk["pass@3"] == 0.0

    def test_pass_at_3_improves_over_pass_at_1(self):
        """pass@3 >= pass@1，因为多次尝试中任一成功即通过。"""
        results = [
            # case_001: 第1次失败，第2次成功
            CaseResult(case_id="case_001", fixed=False, run_index=0),
            CaseResult(case_id="case_001", fixed=True, run_index=1),
            # case_002: 全部失败
            CaseResult(case_id="case_002", fixed=False, run_index=0),
            CaseResult(case_id="case_002", fixed=False, run_index=1),
            CaseResult(case_id="case_002", fixed=False, run_index=2),
        ]
        pk = compute_pass_at_k(results)
        assert pk["pass@1"] == 0.0  # 第1次全失败
        assert pk["pass@3"] == 0.5  # case_001 在第2次通过

    def test_pass_at_3_equals_pass_at_1_with_single_runs(self):
        """所有 case 只跑1次时，pass@3 = pass@1。"""
        results = [
            CaseResult(case_id="case_001", fixed=True, run_index=0),
            CaseResult(case_id="case_002", fixed=False, run_index=0),
        ]
        pk = compute_pass_at_k(results)
        assert pk["pass@1"] == 0.5
        assert pk["pass@3"] == 0.5

    def test_mixed_run_indices(self):
        """不同 case 有不同 run_index 分布。"""
        results = [
            CaseResult(case_id="a", fixed=False, run_index=0),
            CaseResult(case_id="a", fixed=False, run_index=1),
            CaseResult(case_id="a", fixed=True, run_index=2),
            CaseResult(case_id="b", fixed=True, run_index=0),
        ]
        pk = compute_pass_at_k(results)
        assert pk["pass@1"] == 0.5  # a 第1次失败, b 第1次通过
        assert pk["pass@3"] == 1.0  # a 在第3次通过


# ---------------------------------------------------------------------------
# EvalRunner repetitions 集成
# ---------------------------------------------------------------------------


class TestEvalRunnerPassAtK:
    def test_repetitions_1_runs_once(self):
        with tempfile.TemporaryDirectory() as tmp:
            runner = EvalRunner(
                orchestrator_factory=_fake_factory,
                output_dir=Path(tmp),
            )
            report = runner.run_all(["case_001"], repetitions=1)
            assert len(report.cases) == 1
            assert report.cases[0].run_index == 0

    def test_repetitions_3_runs_three_times(self):
        with tempfile.TemporaryDirectory() as tmp:
            runner = EvalRunner(
                orchestrator_factory=_fake_factory,
                output_dir=Path(tmp),
            )
            report = runner.run_all(["case_001"], repetitions=3)
            assert len(report.cases) == 3
            indices = {r.run_index for r in report.cases}
            assert indices == {0, 1, 2}

    def test_repetitions_with_resume(self):
        with tempfile.TemporaryDirectory() as tmp:
            runner = EvalRunner(
                orchestrator_factory=_fake_factory,
                output_dir=Path(tmp),
            )
            # 预填 checkpoint：case_001 的 rep=0 已完成
            runner._save_checkpoint_entry("case_001", variant="", rep=0)

            report = runner.run_all(["case_001", "case_002"], repetitions=2, resume=True)
            # case_001 rep=0 被跳过，只跑 rep=1
            # case_002 跑 rep=0 和 rep=1
            assert len(report.cases) == 3
