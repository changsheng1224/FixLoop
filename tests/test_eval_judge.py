"""JudgeClient + judge_summary 单测（V1.4-Bonus6b）。"""

from __future__ import annotations

from src.eval.judge import JudgeClient, _parse_judge_response
from src.eval.metrics import compute_judge_summary
from src.eval.models import CaseResult


# ---------------------------------------------------------------------------
# _parse_judge_response
# ---------------------------------------------------------------------------


class TestParseJudgeResponse:
    def test_valid_json(self):
        score, reason = _parse_judge_response('{"score":7,"reason":"good fix"}')
        assert score == 7
        assert reason == "good fix"

    def test_json_with_whitespace(self):
        score, reason = _parse_judge_response('  {"score": 5, "reason": "ok"}  ')
        assert score == 5
        assert reason == "ok"

    def test_markdown_code_block(self):
        raw = '```json\n{"score":8,"reason":"perfect"}\n```'
        score, reason = _parse_judge_response(raw)
        assert score == 8
        assert reason == "perfect"

    def test_score_slash_10_fallback(self):
        score, reason = _parse_judge_response("I rate this 8/10. Good job.")
        assert score == 8

    def test_fallback_returns_zero(self):
        score, reason = _parse_judge_response("unparseable free text response")
        assert score == 0
        assert len(reason) <= 200  # capped


# ---------------------------------------------------------------------------
# JudgeClient.compare_with_precision
# ---------------------------------------------------------------------------


class TestCompareWithPrecision:
    def test_aligned(self):
        assert JudgeClient.compare_with_precision(7, 0.7) == "aligned"
        assert JudgeClient.compare_with_precision(8, 0.8) == "aligned"

    def test_aligned_within_one(self):
        assert JudgeClient.compare_with_precision(7, 0.6) == "aligned"
        assert JudgeClient.compare_with_precision(6, 0.7) == "aligned"

    def test_judge_higher(self):
        assert JudgeClient.compare_with_precision(8, 0.5) == "judge_higher"

    def test_judge_lower(self):
        assert JudgeClient.compare_with_precision(3, 0.8) == "judge_lower"


# ---------------------------------------------------------------------------
# compute_judge_summary
# ---------------------------------------------------------------------------


class TestJudgeSummary:
    def test_no_judged_cases(self):
        results = [CaseResult(case_id="a"), CaseResult(case_id="b")]
        assert compute_judge_summary(results) == {}

    def test_single_judged_case(self):
        results = [
            CaseResult(case_id="a", judge_score=7,
                        actual_lines=5, minimal_lines=3),
        ]
        s = compute_judge_summary(results)
        assert s["judged_cases"] == 1
        assert s["avg_judge_score"] == 7.0
        # precision = 3/5 = 0.6, judge=7 → aligned (within 1)
        assert s["aligned_with_precision"] == 1
        assert s["alignment_rate"] == 1.0

    def test_mixed_alignment(self):
        results = [
            CaseResult(case_id="a", judge_score=8, actual_lines=10, minimal_lines=8),
            CaseResult(case_id="b", judge_score=3, actual_lines=5, minimal_lines=4),
            CaseResult(case_id="c"),  # no judge score
        ]
        s = compute_judge_summary(results)
        assert s["judged_cases"] == 2
        # case a: precision=0.8, judge=8 → aligned
        # case b: precision=0.8, judge=3 → judge_lower
        assert s["aligned_with_precision"] == 1
        assert s["alignment_rate"] == 0.5
