"""Two config fields that used to be validated and reported but never enforced.

`budget.soft_cost_limit_usd` / `hard_cost_limit_usd` reached the ledger as
resource names through `effective_budget()` yet were never recorded or checked,
so a run could spend without bound while its snapshot advertised a cap.
`control.verify_skipped_reason` had a reader in `validate_invariants` and no
writer anywhere.
"""

import pytest

from agent_runtime.budget_manager import BudgetManager
from agent_runtime.model_call_budget import ModelBudgetExceededError, invoke_model_call

MODEL = "deepseek-v4-pro"


class _Reply:
    def __init__(self, input_tokens, output_tokens):
        self.text = "ok"
        self.usage = {"input_tokens": input_tokens, "output_tokens": output_tokens}


class _Client:
    """Just enough provider for the dispatch path: a model name and usage."""

    def __init__(self, model=MODEL):
        self.model = model
        self.session_usage = {}


def _expensive_call(input_tokens=1_000_000):
    return lambda: _Reply(input_tokens, 0)


def _invoke(budget, client=None):
    return invoke_model_call(
        _expensive_call(),
        budget=budget,
        client=client or _Client(),
        input_tokens=10,
        output_tokens=10,
        purpose="test",
    )


class TestCostBudget:
    def test_hard_cost_limit_refuses_the_next_call(self):
        budget = BudgetManager({"hard_cost_limit_usd": 0.1, "llm_calls": 0})
        _invoke(budget)
        with pytest.raises(ModelBudgetExceededError, match="cost_limit_exhausted"):
            _invoke(budget)

    def test_usage_keeps_counting_past_the_limit(self):
        """Otherwise the exhausted state could never be observed."""
        budget = BudgetManager({"hard_cost_limit_usd": 0.1, "llm_calls": 0})
        _invoke(budget)
        used = budget.snapshot()["used"]["hard_cost_limit_usd"]
        assert used > 0.1

    def test_zero_means_unlimited_and_never_refuses(self):
        budget = BudgetManager({"hard_cost_limit_usd": 0, "llm_calls": 0})
        for _ in range(3):
            _invoke(budget)
        assert budget.snapshot()["used"]["hard_cost_limit_usd"] > 0
        assert budget.backpressure() == []

    def test_soft_cost_limit_never_refuses(self):
        budget = BudgetManager({"soft_cost_limit_usd": 0.1, "hard_cost_limit_usd": 0})
        for _ in range(3):
            _invoke(budget)
        assert not budget.check("soft_cost_limit_usd", 0.0).allowed

    def test_cost_is_charged_from_the_reported_usage(self):
        from agent_runtime.token_accounting import estimate_cost

        budget = BudgetManager({"hard_cost_limit_usd": 0, "llm_calls": 0})
        _invoke(budget)
        expected = estimate_cost({"input_tokens": 1_000_000, "output_tokens": 0}, model=MODEL)
        assert budget.snapshot()["used"]["hard_cost_limit_usd"] == pytest.approx(expected)

    def test_an_unknown_model_costs_nothing_and_does_not_block(self):
        budget = BudgetManager({"hard_cost_limit_usd": 0.001, "llm_calls": 0})
        client = _Client(model="not-in-the-pricing-table")
        for _ in range(3):
            _invoke(budget, client)
        assert budget.snapshot()["used"]["hard_cost_limit_usd"] == 0


class TestVerifySkippedReason:
    def test_mark_pending_verify_records_the_reason(self):
        from src.repair.verification.termination import mark_pending_verify
        from src.state import RepairState, RepairStatus

        state = RepairState(issue_input="x")
        mark_pending_verify(state, "verification_disabled")
        assert state.status == RepairStatus.PENDING_VERIFY
        assert state.control.verify_skipped is True
        assert state.control.verify_skipped_reason == "verification_disabled"

    def test_the_reason_is_what_satisfies_the_fixed_invariant(self):
        from src.state import RepairPhase, RepairState, RepairStatus

        def fixed_state():
            return RepairState(issue_input="x", status=RepairStatus.FIXED, phase=RepairPhase.DONE)

        without = fixed_state()
        with pytest.raises(ValueError, match="verify_skipped_reason"):
            without.validate_invariants(strict=True)

        # The reason is what the invariant accepts in place of a patch.
        with_reason = fixed_state()
        with_reason.control.verify_skipped_reason = "verification_disabled"
        assert with_reason.validate_invariants(strict=True) == []

    def test_the_reason_survives_a_round_trip(self):
        from src.repair.verification.termination import mark_pending_verify
        from src.state import RepairState

        state = RepairState(issue_input="x")
        mark_pending_verify(state, "verification_disabled")
        restored = RepairState.from_dict(state.to_dict())
        assert restored.control.verify_skipped_reason == "verification_disabled"
