from __future__ import annotations

from agent_runtime.compression_pipeline import validate_compression_contract
from agent_runtime.context_runtime import (
    ContextItem,
    ContextPolicyEngine,
    ContextRequest,
    ContextSelectionResult,
    ObservationStore,
)
from agent_runtime.evidence_extractors import extract_evidence


def test_policy_result_is_deterministic_and_explains_drops():
    request = ContextRequest(phase="patch", token_budget=3, role="patcher")
    items = [
        ContextItem("b", "source", "b", token_cost=2, relevance=0.8),
        ContextItem("a", "source", "a", token_cost=2, relevance=0.8),
        ContextItem("pin", "observation", "p", token_cost=1, hard_pin=True),
    ]
    result = ContextPolicyEngine().select_with_result(items, request)
    assert isinstance(result, ContextSelectionResult)
    assert result.selected_ids == ["pin", "a"]
    assert "b" in result.dropped_ids
    assert result.to_dict()["policy_version"] == "context-policy-v2"


def test_observation_expand_is_scoped_and_budgeted(tmp_path):
    state = {"session_scope": {"workspace_id": "ws-1", "session_id": "s-1"}}
    store = ObservationStore(state, root=str(tmp_path))
    observation = store.put("read_file", {"path": "x.py"}, "line\n" * 20)
    expanded = store.expand_for_context(observation.observation_id, max_tokens=5)
    assert expanded["ok"] is True
    assert expanded["observation_id"] == observation.observation_id
    assert len(expanded["content"]) < 100
    assert any(e["event"] == "expanded" for e in state["observation_audit"])
    store.close()


def test_evidence_extractor_is_bounded_and_generic():
    facts = extract_evidence(
        "test",
        {"path": "tests"},
        "FAILED tests/test_api.py:42\nERROR src/api.py:9",
        source_version="v1",
    )
    assert any(f["kind"] == "verification_failure" for f in facts)
    assert any(f["path"] == "tests/test_api.py" and f["line"] == 42 for f in facts)
    assert all(f.get("source_version") == "v1" for f in facts)


def test_compression_contract_flags_empty_repair_state():
    result = validate_compression_contract(
        [{"role": "tool", "observation_id": "OBS-1"}],
        [{"role": "system", "repair_state": True, "content": ""}],
    )
    assert result["ok"] is False
    assert "empty_repair_state" in result["violations"]

