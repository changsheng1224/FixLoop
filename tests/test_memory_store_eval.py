from agent_runtime.features.memory.store import CanonicalMemoryStore
from src.eval.memory_eval import recall_metrics


def test_canonical_memory_store_persists_memory_and_usage(tmp_path):
    store = CanonicalMemoryStore(str(tmp_path))
    memory = {"memory_id": "MEM-1", "key": "framework", "value": "pytest"}
    store.upsert_memory(memory)
    store.append_usage_event({"memory_id": "MEM-1", "outcome": "supported"})

    assert store.get_memory("MEM-1") == memory
    assert (tmp_path / ".agent" / "memory" / "memory.db").is_file()


def test_memory_recall_metrics_are_deterministic():
    result = recall_metrics(
        [{"memory_id": "M2"}, {"memory_id": "M1"}], ["M1"], k=2
    )

    assert result == {
        "recall_at_k": 1.0,
        "precision_at_k": 0.5,
        "mrr": 0.5,
    }
