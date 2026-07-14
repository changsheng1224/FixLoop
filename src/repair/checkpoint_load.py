"""L2 repair checkpoint 保存/恢复 — --resume-repair 真续跑。

恢复 retry_count/phase/feedback/suspect_locations/blackboard_snapshot。
不恢复 L1 agent session（agent 状态不可序列化）。
"""

from __future__ import annotations

import json
from pathlib import Path

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
    path.write_text(
        json.dumps(state.to_dict(), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return path


def load_repair_checkpoint(repo_root: str, run_id: str) -> "dict | None":
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
        return data
    except (json.JSONDecodeError, OSError):
        return None


__all__ = ["CHECKPOINT_FILENAME", "load_repair_checkpoint", "save_repair_checkpoint"]
