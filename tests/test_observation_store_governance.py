from __future__ import annotations

from agent_runtime.context_runtime import ObservationStore


def test_store_sanitizes_all_persisted_fields_and_supports_memory_expand():
    state = {"id": "s1", "session_scope": {"workspace_id": "w1", "session_id": "s1"}}
    store = ObservationStore(state)
    obs = store.put(
        "probe",
        {"path": "src/a.py"},
        "token=secret-value",
        summary="password=secret-value",
        structured_facts=[{"value": "api_key=secret-value"}],
        provenance={"authorization": "Bearer secret-value"},
    )
    assert obs.redacted is True
    assert "secret-value" not in store.expand(obs.observation_id)
    assert "secret-value" not in obs.summary
    assert store.expand(obs.observation_id).startswith("token:[REDACTED]")


def test_stale_observation_is_immutable_and_new_result_supersedes(tmp_path):
    state = {"id": "s1", "session_scope": {"workspace_id": "w1", "session_id": "s1"}}
    store = ObservationStore(state, str(tmp_path))
    first = store.put("read_file", {"path": "src/a.py"}, "one", source_version="v1")
    assert store.invalidate_paths(["src/a.py"]) == 1
    second = store.put("read_file", {"path": "src/a.py"}, "two", source_version="v1")
    assert first.observation_id != second.observation_id
    assert state["observations"][first.observation_id]["lifecycle"] == "stale"
    assert second.supersedes == first.observation_id
    assert store.expand(first.observation_id) == "one"
    assert store.expand(second.observation_id) == "two"


def test_query_gc_and_sqlite_metadata_are_available(tmp_path):
    state = {"id": "s1", "session_scope": {"workspace_id": "w1", "session_id": "s1"}}
    store = ObservationStore(state, str(tmp_path))
    observation = store.put("read_file", {"path": "a.py"}, "x")
    assert store.query(tool="read_file")[0].observation_id == observation.observation_id
    assert (tmp_path / ".agent" / "observations" / "observations.sqlite3").is_file()
    store.close()
    reloaded = ObservationStore(
        {"id": "s1", "session_scope": {"workspace_id": "w1", "session_id": "s1"}},
        str(tmp_path),
    )
    assert reloaded.get(observation.observation_id) is not None
    store.invalidate_paths(["a.py"])
    assert store.gc(max_records=0)["removed"] >= 1


def test_file_fingerprint_prevents_stale_read_dedup(tmp_path):
    path = tmp_path / "a.py"
    path.write_text("one", encoding="utf-8")
    state = {"id": "s1", "session_scope": {"workspace_id": "w1", "session_id": "s1"}}
    store = ObservationStore(state, str(tmp_path))
    first = store.put("read_file", {"path": "a.py"}, "one")
    path.write_text("two", encoding="utf-8")
    second = store.put("read_file", {"path": "a.py"}, "two")
    assert first.observation_id != second.observation_id


def test_scope_prevents_cross_workspace_expansion(tmp_path):
    first_state = {"id": "s1", "session_scope": {"workspace_id": "w1", "session_id": "s1"}}
    first_store = ObservationStore(first_state, str(tmp_path))
    obs = first_store.put("read_file", {"path": "a.py"}, "private")
    second_state = {"id": "s2", "session_scope": {"workspace_id": "w2", "session_id": "s2"}}
    second_state["observations"] = first_state["observations"]
    second_store = ObservationStore(second_state, str(tmp_path))
    assert second_store.expand(obs.observation_id) == ""


def test_observation_trace_event_has_low_cardinality_metric():
    from agent_runtime.observability.prom_from_trace import record_canonical_event

    class Registry:
        def __init__(self):
            self.calls = []

        def counter_inc(self, name, *, labels):
            self.calls.append((name, labels))

    registry = Registry()
    record_canonical_event(
        {
            "event": "observation_stored",
            "status": "ok",
            "payload": {"tool": "read_file", "observation_id": "OBS-high-cardinality"},
        },
        registry,
    )
    metric = next(item for item in registry.calls if item[0] == "fixloop_observation_events_total")
    assert metric[1]["tool"] == "read_file"
    assert "observation_id" not in metric[1]
