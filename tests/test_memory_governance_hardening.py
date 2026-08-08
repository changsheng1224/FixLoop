from __future__ import annotations

import time

import pytest


def test_canonical_store_uses_schema_version_and_rejects_stale_writes(tmp_path):
    from agent_runtime.features.memory.store import (
        CanonicalMemoryStore,
        MemoryVersionConflictError,
    )

    store = CanonicalMemoryStore(str(tmp_path))
    memory = {"memory_id": "M-cas", "key": "rule", "value": "v1"}
    assert store.upsert_memory(memory) == 1
    memory["value"] = "v2"
    assert store.upsert_memory(memory, expected_version=1) == 2
    with pytest.raises(MemoryVersionConflictError):
        store.upsert_memory({"memory_id": "M-cas", "value": "stale"}, expected_version=1)
    assert store.get_memory("M-cas")["version"] == 2
    assert store.integrity_check() == "ok"


def test_governed_memory_redacts_lineage_and_retention(tmp_path):
    from agent_runtime.features.memory.candidate import Candidate
    from agent_runtime.features.memory.governance import MemoryGovernanceService

    service = MemoryGovernanceService({}, repo_root=str(tmp_path))
    memory = service.ingest(
        Candidate(
            topic="project-conventions",
            key="api_key",
            value="api_key=super-secret",
            source="tool token=another-secret",
            source_observation_ids=["OBS-1"],
            source_run_id="RUN-1",
            retention_days=1,
        ),
        scope="repository",
    )
    raw = service.inspect(memory.memory_id)
    assert "super-secret" not in raw["value"]
    assert "another-secret" not in raw["source"]
    assert raw["source_observation_ids"] == ["OBS-1"]
    assert raw["source_run_id"] == "RUN-1"
    assert raw["expires_at"] > time.time()


def test_expired_memory_forget_removes_canonical_usage(tmp_path):
    from agent_runtime.features.memory.candidate import Candidate
    from agent_runtime.features.memory.governance import MemoryGovernanceService

    state = {}
    service = MemoryGovernanceService(state, repo_root=str(tmp_path))
    memory = service.ingest(
        Candidate("project-conventions", "temporary", "temporary", retention_days=1),
        scope="repository",
    )
    service.record_recall([memory.memory_id], task_id="task-1")
    service.registry[memory.memory_id]["expires_at"] = 1
    assert service.purge_expired(now=2) == 1
    assert service.inspect(memory.memory_id) is None
    assert service.store.get_memory(memory.memory_id) is None
    assert not any(
        event.get("memory_id") == memory.memory_id
        for event in state.get("memory_usage_events", [])
    )


def test_recall_exposes_score_breakdown_and_stage_attribution():
    from agent_runtime.features.memory.governance import MemoryGovernanceService

    state = {}
    service = MemoryGovernanceService(state, task_id="task-1")

    class Candidate:
        key = "test.command"
        value = "pytest -q"
        source = "tool"
        source_type = "tool"
        confidence = 0.9
        evidence_refs = ["OBS-1"]

    memory = service.ingest(Candidate(), scope="task")
    results = service.recall("pytest", task_id="task-1")
    assert results[0]["memory_id"] == memory.memory_id
    assert results[0]["score_breakdown"]["lexical"] > 0
    assert results[0]["matched_tokens"] == ["pytest"]
    event = service.record_usage_stage(
        memory.memory_id,
        usage="applied",
        stage="patch",
        task_id="task-1",
        turn_id="turn-2",
        prompt_id="prompt-2",
        context_item_id=memory.memory_id,
        cited=True,
        evidence_refs=["OBS-1"],
    )
    assert event["stage"] == "patch"
    assert event["turn_id"] == "turn-2"
    assert event["prompt_id"] == "prompt-2"
    assert event["cited"] is True


def test_durable_projection_has_stable_canonical_id_and_redaction(tmp_path):
    from agent_runtime.features.memory.durable import DurableMemoryStore

    store = DurableMemoryStore(str(tmp_path))
    store.promote([("project-conventions", "token=do-not-leak")])
    results = store.retrieval("token", limit=1)
    assert results[0]["memory_id"].startswith("DUR-")
    assert "do-not-leak" not in results[0]["text"]
    canonical = store._canonical.get_memory(results[0]["memory_id"])
    assert canonical["kind"] == "durable_projection"
    assert canonical["status"] == "durable"
