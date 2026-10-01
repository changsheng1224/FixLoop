"""Owner-side tool observations; execution and repair decisions live elsewhere."""

import json
import threading
from dataclasses import dataclass

from agent_runtime.context_runtime import Observation, ObservationStore
from agent_runtime.evidence_extractors import evidence_provenance, extract_evidence
from agent_runtime.repair_runtime import observation_from_result


@dataclass(frozen=True)
class RecordedObservation:
    stored: Observation
    observation: dict


class ToolObservationRecorder:
    def __init__(self, *, session, root, state_root, emit, on_changed_paths, on_retrieval):
        self.session = session
        self.root = root
        self.state_root = state_root
        self.emit = emit
        self.on_changed_paths = on_changed_paths
        self.on_retrieval = on_retrieval
        self.owner_thread = threading.get_ident()

    def record(
        self,
        call,
        result,
        *,
        duration_ms,
        metadata,
        source_version,
        idempotency_key,
        call_context=None,
    ):
        if threading.get_ident() != self.owner_thread:
            raise RuntimeError("observation_recording_requires_owner")
        observation = observation_from_result(call, result, duration_ms)
        projection = observation.to_dict()
        projection["source"] = call.source.value
        if call_context is not None:
            projection.update(
                turn_id=call_context.turn_id,
                batch_id=call_context.batch_id,
                ordinal=call_context.ordinal,
                receipt=dict(result.receipt),
            )
        result_text = result.content if hasattr(result, "content") else str(result)
        source_version = str(metadata.get("source_version") or source_version or "")
        raw_result = metadata.get("raw_result")
        raw_observation = (
            json.dumps(raw_result, ensure_ascii=False, default=str)
            if raw_result is not None
            else result_text
        )
        facts = list(metadata.get("structured_facts") or [])
        facts.extend(
            extract_evidence(call.name, call.arguments, result_text, source_version=source_version)
        )
        facts.append(
            {
                "status": observation.status,
                "error_code": observation.failure_class,
                "retryable": observation.retryable,
                "changed_files": observation.changed_files,
            }
        )
        store = ObservationStore(self.session, root=self.root, state_root=self.state_root)
        try:
            # Invalidate before publishing the new record, including non-patch writes.
            if observation.changed_files:
                store.invalidate_paths(observation.changed_files, "tool_write_changed_dependency")
                self.on_changed_paths(call.name)
            stored = store.put(
                call.name,
                call.arguments,
                raw_observation,
                summary=result_text[:500],
                source_version=source_version,
                structured_facts=facts,
                provenance=evidence_provenance(
                    call.name, call.arguments, source_version=source_version
                ),
                dependencies=list(observation.changed_files),
                error_code=observation.failure_class,
                status=observation.status,
                evidence_refs=list(observation.evidence_ids),
                source_dependencies=(
                    dict(metadata.get("source_dependencies") or {})
                    if "source_dependencies" in metadata
                    else None
                ),
                retrieval_query_id=str(
                    (metadata.get("retrieval_result") or {}).get("query_id", "")
                ),
                retrieval_result=metadata.get("retrieval_result"),
                redact=True,
            )
        finally:
            # Also close on failed persistence; do not publish result references on failure.
            store.close()
        projection["observation_id"] = stored.observation_id
        projection["raw_ref"] = stored.raw_ref
        metadata["observation_id"] = stored.observation_id
        metadata["artifact_ref"] = stored.raw_ref
        self.on_retrieval(
            call.name, call.arguments, metadata.get("retrieval_result"), stored.observation_id
        )
        # Only the matching action gains a reference, after durable storage succeeds.
        for action in reversed(self.session.get("action_ledger", [])):
            if action.get("idempotency_key") == idempotency_key:
                action["result_ref"] = stored.observation_id
                action["artifact_ref"] = stored.raw_ref
                break
        self.emit(
            "observation_stored",
            {
                "observation_id": stored.observation_id,
                "tool": call.name,
                "status": stored.status,
                "deduplicated": False,
                "redacted": stored.redacted,
            },
        )
        if metadata.get("provider") == "mcp":
            projection["provider"] = "mcp"
            projection["server"] = metadata.get("mcp_server", "")
        self.session["_last_tool_observation"] = projection
        self.session.setdefault("tool_observations", []).append(projection)
        self.session["tool_observations"] = self.session["tool_observations"][-50:]
        return RecordedObservation(stored, projection)
