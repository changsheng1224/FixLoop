from agent_runtime.latency_controller import LatencySLOController
from agent_runtime.policy import DegradationPolicy, LatencySLOPolicy


def test_latency_slo_triggers_adaptive_degradation():
    controller = LatencySLOController(
        LatencySLOPolicy(model_p95_ms=100), DegradationPolicy(max_output_floor=256)
    )
    controller.record("model", 150)
    decision = controller.decide(remaining_s=120, max_output_tokens=1000)
    assert decision["degraded"]
    assert "skip_optional_context" in decision["actions"]
    assert decision["max_output_tokens"] == 500


def test_latency_deadline_is_a_degradation_signal():
    controller = LatencySLOController(
        LatencySLOPolicy(), DegradationPolicy(max_output_floor=256)
    )
    decision = controller.decide(remaining_s=10, max_output_tokens=1000)
    assert decision["degraded"]
    assert "deadline_tight" in decision["reasons"]
