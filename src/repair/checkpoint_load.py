"""L2 repair checkpoint 保存/恢复 — --resume-repair 真续跑。

恢复 retry_count/phase/feedback/suspect_locations/blackboard_snapshot。
不恢复 L1 agent session（agent 状态不可序列化）。
"""

from __future__ import annotations

import json
import uuid
from pathlib import Path

from agent_runtime.session_contract import CheckpointEnvelope, workspace_manifest

CHECKPOINT_FILENAME = "repair_checkpoint.json"


def save_repair_checkpoint(state, repo_root: str) -> Path:
    """将 RepairState 保存到 .agent/runs/<run_id>/repair_checkpoint.json。

    保存关键字段：retry_count/phase/feedback/suspect_locations/blackboard_snapshot。
    """
    from src.state import RepairState as _RepairState

    if not isinstance(state, _RepairState):
        raise TypeError(f"Expected RepairState, got {type(state)}")

    run_id = state.repair_run_id
    if not run_id:
        raise ValueError("RepairState.repair_run_id 未设置")

    path = Path(repo_root) / ".agent" / "runs" / run_id / CHECKPOINT_FILENAME
    path.parent.mkdir(parents=True, exist_ok=True)
    state.checkpoint_sequence = int(getattr(state, "checkpoint_sequence", 0) or 0) + 1
    state.checkpoint_id = "cp-" + uuid.uuid4().hex[:16]
    state_payload = state.to_dict()
    state_payload["workspace_manifest"] = workspace_manifest(repo_root)
    envelope = CheckpointEnvelope(
        checkpoint_id=state_payload["checkpoint_id"],
        sequence=state_payload["checkpoint_sequence"],
        trigger="repair_progress",
        safe_point=str(state_payload.get("phase") or "repair"),
        identity={
            "task_id": state_payload.get("repair_run_id", ""),
            "run_id": state_payload.get("repair_run_id", ""),
            "attempt_id": str(state_payload.get("attempt", 0)),
        },
        runtime_control={
            "retry_count": state_payload.get("retry_count", 0),
            "max_retries": state_payload.get("max_retries", 0),
            "tool_budget": state_payload.get("tool_budget", {}),
            "phase": state_payload.get("phase", ""),
            "state_revision": state_payload.get("state_revision", 0),
        },
        task_state=state_payload,
        context_manifest={
            "evidence": state_payload.get("evidence", []),
            "hypotheses": state_payload.get("hypotheses", []),
        },
        workspace_manifest=state_payload["workspace_manifest"],
        action_ledger=list(state_payload.get("action_ledger", []) or []),
        side_effects=list(state_payload.get("side_effects", []) or []),
        terminal_status=str(state_payload.get("status", "pending")),
    ).seal()
    state_payload["checkpoint_envelope"] = envelope.to_dict()
    state_payload["checkpoint_checksum"] = envelope.checksum
    tmp = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    tmp.write_text(json.dumps(state_payload, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)
    return path


def load_repair_checkpoint(repo_root: str, run_id: str) -> dict | None:
    """从 .agent/runs/<run_id>/repair_checkpoint.json 加载 RepairState dict。

    Returns:
        RepairState dict 或 None（文件不存在/损坏时）。
    """
    path = Path(repo_root) / ".agent" / "runs" / run_id / CHECKPOINT_FILENAME
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict) or "retry_count" not in data:
            return None
        envelope_raw = data.get("checkpoint_envelope")
        if isinstance(envelope_raw, dict):
            envelope = CheckpointEnvelope.from_dict(envelope_raw)
            if not envelope.verify() or data.get("checkpoint_checksum") != envelope.checksum:
                return None
            # The envelope task_state is authoritative.  Reject a checkpoint
            # whose duplicated top-level fields were modified independently.
            task_state = envelope.task_state or {}
            for key, value in task_state.items():
                if key in data and json.dumps(data[key], sort_keys=True, default=str) != json.dumps(
                    value, sort_keys=True, default=str
                ):
                    return None
            data.update(task_state)
        return data
    except (json.JSONDecodeError, OSError, TypeError, ValueError):
        return None


__all__ = ["CHECKPOINT_FILENAME", "load_repair_checkpoint", "save_repair_checkpoint"]
