"""统一错误码 taxonomy 单测：枚举 + stop_reason 映射。"""

from agent_runtime.error_codes import (
    STOP_REASON_TO_ERROR_CODE,
    FixLoopErrorCode,
)


class TestFixLoopErrorCode:
    def test_gate_codes_exist(self):
        assert FixLoopErrorCode.GATE5_DUPLICATE == "GATE5_DUPLICATE"
        assert FixLoopErrorCode.GATE7_QUOTA_EXCEEDED == "GATE7_QUOTA_EXCEEDED"

    def test_phase_codes_exist(self):
        assert FixLoopErrorCode.PHASE_TIMEOUT == "PHASE_TIMEOUT"

    def test_semantic_codes_exist(self):
        assert FixLoopErrorCode.SEMANTIC_DRIFT == "SEMANTIC_DRIFT"

    def test_context_codes_exist(self):
        assert FixLoopErrorCode.CONTEXT_OVERFLOW == "CONTEXT_OVERFLOW"

    def test_budget_codes_exist(self):
        assert FixLoopErrorCode.BUDGET_EXHAUSTED == "BUDGET_EXHAUSTED"

    def test_api_codes_exist(self):
        assert FixLoopErrorCode.API_ERROR == "API_ERROR"
        assert FixLoopErrorCode.RATE_LIMITED == "RATE_LIMITED"
        assert FixLoopErrorCode.CIRCUIT_BREAKER == "CIRCUIT_BREAKER"


class TestStopReasonMapping:
    def test_known_stop_reasons_mapped(self):
        for reason in (
            "context_overflow",
            "budget_exhausted",
            "step_timeout",
            "circuit_breaker",
            "api_error",
        ):
            code = STOP_REASON_TO_ERROR_CODE.get(reason)
            assert code is not None, f"{reason} should have error code"

    def test_context_overflow_maps_correctly(self):
        assert STOP_REASON_TO_ERROR_CODE["context_overflow"] == FixLoopErrorCode.CONTEXT_OVERFLOW

    def test_budget_exhausted_maps_correctly(self):
        assert STOP_REASON_TO_ERROR_CODE["budget_exhausted"] == FixLoopErrorCode.BUDGET_EXHAUSTED

    def test_gate_enum_values_unique(self):
        gate_codes = [c for c in FixLoopErrorCode if c.startswith("GATE")]
        assert len(gate_codes) == len(set(gate_codes))
