"""自适应预算：规则强锚跳过 LLM localize、enrich 超时与 patch 步数裁剪。"""

from __future__ import annotations

from src.repair.adaptive_budget import (
    advise_budget,
    localize_enrich_timeout_s,
    recommend_patcher_steps,
    should_skip_llm_localize,
)
from src.state import RepairState, SuspectLocation


def _sus(path: str) -> SuspectLocation:
    return SuspectLocation(file_path=path, start_line=1, end_line=10, reason="rule")


def test_skip_llm_when_grounded_with_tests():
    rules = [_sus("pkg/mod.py"), _sus("pkg/other.py")]
    assert should_skip_llm_localize(
        rules, grounded=True, related_tests=["tests/test_mod.py::t"]
    )


def test_skip_llm_when_grounded_single_plus_test():
    rules = [_sus("pkg/mod.py")]
    assert should_skip_llm_localize(
        rules, grounded=True, related_tests=["tests/test_mod.py::t"]
    )


def test_no_skip_when_ungrounded():
    rules = [_sus("pkg/mod.py"), _sus("pkg/other.py")]
    assert not should_skip_llm_localize(
        rules, grounded=False, related_tests=["tests/test_mod.py::t"]
    )


def test_enrich_timeout_stronger_when_grounded(monkeypatch):
    monkeypatch.delenv("FIXLOOP_LOCALIZE_ENRICH_S", raising=False)
    monkeypatch.delenv("FIXLOOP_LOCALIZE_ENRICH_WEAK_S", raising=False)
    strong = localize_enrich_timeout_s(grounded=True, rule_count=2)
    weak = localize_enrich_timeout_s(grounded=False, rule_count=0)
    assert strong == 25.0
    assert weak == 45.0
    assert strong < weak


def test_advise_budget_skip_sets_reason():
    state = RepairState(issue_input="x")
    advice = advise_budget(
        state,
        rule_suspects=[_sus("a.py"), _sus("b.py")],
        grounded=True,
        related_tests=["t.py"],
    )
    assert advice.skip_llm_localize is True
    assert advice.reason == "rule_grounded_skip_llm_localize"
    d = advice.to_dict()
    assert d["skip_llm_localize"] is True


def test_recommend_patcher_steps_trims_on_zero_gain():
    state = RepairState(issue_input="x")
    state.node_timings["info_gain"] = {"zero_gain_streak": 2}
    trimmed = recommend_patcher_steps(state, base_steps=12)
    assert trimmed == 8  # 12 - 4, floor 6


def test_recommend_patcher_steps_trims_on_negated():
    state = RepairState(issue_input="x")
    state.node_timings["failure_ledger"] = {
        "negated_files": ["a.py", "b.py"],
    }
    trimmed = recommend_patcher_steps(state, base_steps=12)
    assert trimmed == 10  # 12 - 2


def test_short_repair_base_then_gain_penalty():
    state = RepairState(issue_input="x")
    state.node_timings["info_gain"] = {"zero_gain_streak": 2}
    steps = recommend_patcher_steps(state, base_steps=16, short_repair=True)
    assert steps == 12  # max(16,16) then -4
