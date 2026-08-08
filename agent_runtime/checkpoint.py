"""Checkpoint 机制：跨轮恢复 + 文件变更检测。

每次 ask() 结束时自动创建 checkpoint，下次 resume 时检测：
- 文件是否被外部修改（freshness hash）
- runtime 身份是否变化（cwd/model/approval/tools）

触发点规范 (CheckpointTrigger)：
- step_end:    工具执行成功后，可 mid-loop resume
- user_cancel: Ctrl+C / /cancel，记录 in_flight_tool
- ask_end:     ask() 正常结束，仅 full-session resume
"""

import hashlib
import time
from pathlib import Path
from typing import Literal

from agent_runtime.session_contract import (
    CheckpointEnvelope,
    SessionIdentity,
    compare_workspace_manifest,
    workspace_manifest,
)

CHECKPOINT_SCHEMA_VERSION = "1.0"
CheckpointTrigger = Literal["step_end", "user_cancel", "ask_end"]
VALID_TRIGGERS: frozenset[str] = frozenset({"step_end", "user_cancel", "ask_end"})

# 组成 runtime 身份的配置字段
RUNTIME_IDENTITY_KEYS = [
    "cwd",
    "provider",
    "model",
    "approval",
    "max_steps",
    "prompt_assets_fingerprint",
    "tools_signature",
]


def current_runtime_identity(agent) -> dict:
    """捕获当前 Agent 的 runtime 身份快照。

    Args:
        agent: Agent 实例。

    Returns:
        {cwd, provider, model, approval, max_steps, tools_signature} 字典。
    """
    return {
        "cwd": agent._cwd,
        "provider": agent.config.provider,
        "model": agent.config.model,
        "approval": agent.config.approval,
        "max_steps": agent.config.max_steps,
        "tools_signature": agent._prefix.tool_signature,
        "prompt_assets_fingerprint": getattr(agent._prefix, "assets_fingerprint", "") or "",
    }


def create_checkpoint(
    agent,
    task_state,
    user_message: str,
    trigger: CheckpointTrigger = "ask_end",
    *,
    last_tool: str = "",
    in_flight_tool: str = "",
    step_payload: dict | None = None,
) -> dict:
    """创建检查点，记录当前状态。

    Args:
        agent: Agent 实例。
        task_state: TaskState 实例。
        user_message: 当前用户输入（作为 goal）。
        trigger: 触发原因（step_end/user_cancel/ask_end）。
        last_tool: 最近执行成功的工具名（step_end 时有效）。
        in_flight_tool: cancel 时正在执行中的工具名（user_cancel 时有效）。
        step_payload: step-level resume 所需的工具输入、输出和副作用信息。

    Raises:
        ValueError: trigger 不在 VALID_TRIGGERS 中。

    Returns:
        checkpoint 字典。
    """
    if trigger not in VALID_TRIGGERS:
        raise ValueError(f"非法 trigger '{trigger}'，允许值: {sorted(VALID_TRIGGERS)}")

    # 从 working memory 提取关键文件
    key_files = {}
    memory = agent.session.get("memory", {})
    working = memory.get("working", {})
    for path in working.get("recent_files", [])[-5:]:
        freshness = _file_freshness(agent._cwd, path)
        if freshness:
            key_files[path] = freshness

    sequence = (
        max(
            int(agent.session.get("checkpoint_sequence", 0) or 0),
            len(agent.session.get("checkpoints", []) or []),
        )
        + 1
    )
    previous = (agent.session.get("checkpoints", []) or [])[-1:]
    previous_id = str((previous[0] or {}).get("checkpoint_id", "")) if previous else ""
    scope = dict(agent.session.get("session_scope") or {})
    identity = SessionIdentity(
        session_id=str(agent.session.get("id", "")),
        user_id=str(scope.get("user_id", "")),
        workspace_id=str(scope.get("workspace_id", "")),
        task_id=str(getattr(task_state, "task_id", "") or ""),
        run_id=str(getattr(task_state, "run_id", "") or ""),
        attempt_id=str(getattr(task_state, "attempt_id", "") or ""),
        parent_run_id=str(getattr(task_state, "l2_repair_run_id", "") or ""),
    )
    control = _runtime_control_snapshot(agent, task_state)
    manifest = workspace_manifest(agent._cwd, key_files=list(key_files))
    checkpoint = {
        "schema_version": CHECKPOINT_SCHEMA_VERSION,
        "checkpoint_envelope_version": "2.0",
        "checkpoint_id": "cp-"
        + hashlib.sha256(f"{identity.run_id}:{sequence}:{time.time_ns()}".encode()).hexdigest()[
            :16
        ],
        "sequence": sequence,
        "parent_checkpoint_id": previous_id,
        "created_at": time.time(),
        "trigger": trigger,
        "run_id": task_state.run_id,
        "current_goal": user_message[:200],
        "blocker": "",
        "next_step": "",
        "key_files": key_files,
        "runtime_identity": current_runtime_identity(agent),
        "tool_steps": task_state.tool_steps,
        "stop_reason": task_state.stop_reason,
        "last_tool": last_tool,
        "phase": str(getattr(task_state, "phase", "") or ""),
        "turn": int(getattr(task_state, "turn", 0) or 0),
        "repair_context": dict(working.get("repair_context", {}) or {}),
        "evidence_ledger": list(working.get("evidence_ledger", []) or [])[-20:],
        "tool_budget": (
            agent._repair_budget.summary()
            if getattr(agent, "_repair_budget", None) is not None
            else {}
        ),
        "last_tool_observation": dict(agent.session.get("_last_tool_observation", {}) or {}),
        "workspace_fingerprint": _workspace_fingerprint(agent._cwd),
        "identity": identity.to_dict(),
        "runtime_control": control,
        "workspace_manifest": manifest,
        "task_state": task_state.to_dict(),
        "side_effects": list(agent.session.get("side_effects", []) or []),
        "terminal_status": str(getattr(task_state, "status", "running") or "running"),
        "config_snapshot": (agent.config.snapshot() if hasattr(agent.config, "snapshot") else {}),
    }
    from agent_runtime.context_runtime import build_context_manifest

    runtime_memory = agent.session.get("memory", {})
    checkpoint["context_manifest"] = build_context_manifest(
        runtime_memory,
        workspace_fingerprint=checkpoint["workspace_fingerprint"],
        context_metadata=dict(agent.session.get("context_manifest", {}) or {}),
    )
    observations = agent.session.get("observations", {}) or {}
    checkpoint["observation_manifest"] = [
        {
            "observation_id": str(oid),
            "checksum": str((raw or {}).get("checksum", "")),
            "raw_ref": str((raw or {}).get("raw_ref", "")),
            "lifecycle": str((raw or {}).get("lifecycle", "active")),
        }
        for oid, raw in list(observations.items())[-100:]
        if isinstance(raw, dict)
    ]
    checkpoint["context_contract"] = {
        "schema_version": str(checkpoint["context_manifest"].get("schema_version", "")),
        "policy_version": str(checkpoint["context_manifest"].get("policy_version", "")),
        "projection_hash": str(checkpoint["context_manifest"].get("projection_hash", "")),
        "selected_context_ids": list(
            checkpoint["context_manifest"].get("selected_context_ids", [])
        ),
    }
    checkpoint["action_ledger"] = list(agent.session.get("action_ledger", []) or [])[-100:]
    if trigger == "user_cancel" and in_flight_tool:
        checkpoint["in_flight_tool"] = in_flight_tool
        checkpoint["in_flight_action"] = dict(agent.session.get("_in_flight_action", {}) or {})
        checkpoint["side_effect_status"] = "uncertain"
    if step_payload:
        checkpoint.update(step_payload)

    task_state.checkpoint_id = checkpoint["checkpoint_id"]
    task_state.checkpoint_sequence = sequence
    task_state.parent_checkpoint_id = previous_id
    checkpoint["task_state"] = task_state.to_dict()

    envelope = CheckpointEnvelope(
        checkpoint_id=checkpoint["checkpoint_id"],
        sequence=sequence,
        trigger=trigger,
        safe_point="tool_step" if trigger == "step_end" else "full_session",
        identity=identity.to_dict(),
        runtime_control=control,
        task_state=checkpoint.get("task_state", {}),
        context_manifest=checkpoint.get("context_manifest", {}),
        workspace_manifest=manifest,
        action_ledger=checkpoint.get("action_ledger", []),
        side_effects=checkpoint.get("side_effects", []),
        observation_manifest=checkpoint.get("observation_manifest", []),
        terminal_status=checkpoint["terminal_status"],
        parent_checkpoint_id=previous_id,
    ).seal()
    checkpoint["checkpoint_envelope"] = envelope.to_dict()
    checkpoint["checksum"] = envelope.checksum
    checkpoint["safe_point"] = envelope.safe_point

    # 存入 session
    agent.session.setdefault("checkpoints", []).append(checkpoint)
    agent.session["checkpoints"] = agent.session["checkpoints"][-100:]
    agent.session["checkpoint_sequence"] = sequence
    loop = getattr(agent, "_loop", None)
    if loop is not None and hasattr(loop, "_emit"):
        try:
            loop._emit(
                "checkpoint_committed",
                {
                    "checkpoint_id": checkpoint["checkpoint_id"],
                    "sequence": sequence,
                    "trigger": trigger,
                    "safe_point": envelope.safe_point,
                },
            )
        except Exception:
            pass
    return checkpoint


def _runtime_control_snapshot(agent, task_state) -> dict:
    budget = getattr(agent, "_repair_budget", None)
    deadline = getattr(agent, "_repair_deadline", None)
    loop = getattr(agent, "_loop", None)
    return {
        "max_steps": int(
            getattr(loop, "max_steps", 0) or getattr(agent.config, "max_steps", 0) or 0
        ),
        "budget": budget.snapshot() if budget is not None else {},
        "budget_manager": (
            getattr(loop, "_budget_manager").snapshot()
            if loop is not None and getattr(loop, "_budget_manager", None) is not None
            else {}
        ),
        "deadline": deadline.snapshot() if deadline is not None else {"remaining_s": None},
        "retry_count": int(getattr(loop, "_retry_count", 0) or 0),
        "no_progress_steps": int(getattr(loop, "_no_progress_steps", 0) or 0),
        "json_retry_count": int(getattr(loop, "_json_retry_count", 0) or 0),
        "empty_retries": int(getattr(loop, "_empty_retries", 0) or 0),
        "turn": int(getattr(task_state, "turn", 0) or 0),
        "tool_steps": int(getattr(task_state, "tool_steps", 0) or 0),
    }


def evaluate_resume_state(agent) -> dict:
    """评估 resume 状态。返回 status + stale_files + identity_diff + last_checkpoint。"""
    checkpoints = agent.session.get("checkpoints", [])
    if not checkpoints:
        return _emit_resume_result(agent, _resume_result("no-checkpoint"))

    last = checkpoints[-1]

    envelope_raw = last.get("checkpoint_envelope")
    if isinstance(envelope_raw, dict):
        try:
            envelope = CheckpointEnvelope.from_dict(envelope_raw)
            if not envelope.verify():
                return _emit_resume_result(agent, _resume_result("integrity-failure", last))
            if last.get("identity") and last.get("identity") != envelope.identity:
                return _emit_resume_result(agent, _resume_result("integrity-failure", last))
            for field in (
                "task_state",
                "context_manifest",
                "workspace_manifest",
                "action_ledger",
                "side_effects",
                "observation_manifest",
            ):
                if field in last and last.get(field) != getattr(envelope, field):
                    return _emit_resume_result(agent, _resume_result("integrity-failure", last))
        except (TypeError, ValueError):
            return _emit_resume_result(agent, _resume_result("integrity-failure", last))

    # Schema 版本检查
    if last.get("schema_version") != CHECKPOINT_SCHEMA_VERSION:
        return _emit_resume_result(agent, _resume_result("schema-mismatch", last))

    result = _resume_result("full-valid", last)

    saved_context = last.get("context_contract") or last.get("context_manifest") or {}
    current_context = agent.session.get("context_manifest", {}) or {}
    context_diff = []
    if current_context:
        for key in ("schema_version", "policy_version"):
            if saved_context.get(key) and current_context.get(key) != saved_context.get(key):
                context_diff.append(key)
        saved_ids = set(saved_context.get("selected_context_ids", []) or [])
        current_ids = set(current_context.get("selected_context_ids", []) or [])
        if saved_ids and current_ids and saved_ids != current_ids:
            context_diff.append("selected_context_ids")
    result["context_diff"] = context_diff

    saved_manifest = last.get("workspace_manifest")
    if isinstance(saved_manifest, dict) and saved_manifest:
        manifest_diff = compare_workspace_manifest(
            saved_manifest,
            workspace_manifest(
                agent._cwd,
                key_files=list((saved_manifest.get("files") or {}).keys()),
            ),
        )
        result["workspace_manifest_diff"] = manifest_diff
        result["stale_files"].extend(manifest_diff["stale_files"])
        result["identity_diff"].extend(manifest_diff["identity_diff"])

    observation_diff = []
    current_observations = agent.session.get("observations", {}) or {}
    for item in last.get("observation_manifest", []) or []:
        current = current_observations.get(item.get("observation_id"), {})
        if not current:
            observation_diff.append(f"{item.get('observation_id')} (missing)")
        elif item.get("checksum") and current.get("checksum") != item.get("checksum"):
            observation_diff.append(f"{item.get('observation_id')} (checksum)")
        elif current.get("lifecycle") in {"stale", "invalidated"}:
            observation_diff.append(f"{item.get('observation_id')} (stale)")
    result["observation_diff"] = observation_diff

    # 检查 key_files freshness
    for path, saved_hash in last.get("key_files", {}).items():
        current = _file_freshness(agent._cwd, path)
        if current and current != saved_hash:
            result["stale_files"].append(path)
        elif not current:
            result["stale_files"].append(f"{path} (deleted)")

    # 检查 runtime identity
    current_id = current_runtime_identity(agent)
    saved_id = last.get("runtime_identity", {})
    for key in RUNTIME_IDENTITY_KEYS:
        if current_id.get(key) != saved_id.get(key):
            result["identity_diff"].append(key)
    saved_identity = last.get("identity") or {}
    session_scope = agent.session.get("session_scope") or {}
    for key in ("session_id", "user_id", "workspace_id"):
        expected = saved_identity.get(key, "")
        current = agent.session.get("id", "") if key == "session_id" else session_scope.get(key, "")
        if expected and str(expected) != str(current):
            result["identity_diff"].append(key)

    _validate_step_effects(agent, last, result)

    # 判定状态
    if result["stale_files"] or result["identity_diff"]:
        result["status"] = "workspace-mismatch" if result["identity_diff"] else "partial-stale"
    elif observation_diff or context_diff:
        result["status"] = "partial-stale"
    elif (
        last.get("trigger") == "step_end"
        and last.get("resume_kind") == "tool_step"
        and last.get("next_user_message")
    ):
        result["status"] = "step-resumable"
        result["resume_observation"] = {
            "tool": last.get("tool", ""),
            "tool_args": last.get("tool_args", {}),
            "tool_result": last.get("tool_result", ""),
            "next_user_message": last.get("next_user_message", ""),
            "step_index": last.get("step_index", 0),
            "task_state": last.get("task_state", {}),
            "effects": last.get("effects", []),
        }

    return _emit_resume_result(agent, result)


def _emit_resume_result(agent, result: dict) -> dict:
    loop = getattr(agent, "_loop", None)
    if loop is not None and hasattr(loop, "_emit"):
        try:
            loop._emit(
                "resume_evaluated",
                {
                    "status": result.get("status", ""),
                    "stale_files": list(result.get("stale_files", []) or []),
                    "identity_diff": list(result.get("identity_diff", []) or []),
                    "context_diff": list(result.get("context_diff", []) or []),
                },
            )
        except Exception:
            pass
    return result


def _resume_result(status: str, checkpoint: dict | None = None) -> dict:
    return {
        "status": status,
        "stale_files": [],
        "identity_diff": [],
        "context_diff": [],
        "last_checkpoint": checkpoint,
    }


def _file_freshness(root: str, path: str) -> str:
    """计算内容 freshness hash，避免 mtime 在复制/恢复场景下失真。"""
    return file_content_hash(root, path)


def _workspace_fingerprint(root: str) -> str:
    """Cheap workspace identity for resume diagnostics."""
    try:
        root_path = Path(root)
        parts = []
        for path in sorted(root_path.rglob("*")):
            if path.is_file() and ".agent" not in path.parts and ".git" not in path.parts:
                parts.append(f"{path.relative_to(root_path)}:{path.stat().st_size}")
                if len(parts) >= 500:
                    break
        return hashlib.sha256("|".join(parts).encode()).hexdigest()[:16]
    except OSError:
        return ""


def _legacy_file_freshness(root: str, path: str) -> str:
    """Legacy mtime helper retained for old checkpoint readers."""
    try:
        p = Path(root) / path
        if p.exists():
            stat = p.stat()
            raw = f"{stat.st_mtime}:{stat.st_size}"
            return hashlib.sha256(raw.encode()).hexdigest()[:16]
    except OSError:
        pass
    return ""


def file_content_hash(root: str, path: str) -> str:
    """计算文件内容 SHA256；文件不存在或不可读时返回空字符串。"""
    try:
        p = Path(root) / path
        if p.is_file():
            return hashlib.sha256(p.read_bytes()).hexdigest()
    except OSError:
        pass
    return ""


def _validate_step_effects(agent, checkpoint: dict, result: dict) -> None:
    """校验写类工具的 post-state hash，避免重复/错误续跑。"""
    if checkpoint.get("resume_kind") != "tool_step":
        return
    for effect in checkpoint.get("effects", []) or []:
        path = str(effect.get("path", "") or "")
        if not path:
            continue
        expected = str(effect.get("post_hash", "") or "")
        if not expected:
            continue
        current = file_content_hash(agent._cwd, path)
        if current != expected:
            result["stale_files"].append(path if current else f"{path} (deleted)")
