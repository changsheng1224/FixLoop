"""Checkpoint 机制：跨轮恢复 + 文件变更检测。

每次 ask() 结束时自动创建 checkpoint，下次 resume 时检测：
- 文件是否被外部修改（freshness hash）
- runtime 身份是否变化（cwd/model/approval/tools）
"""

import hashlib
import time
from pathlib import Path

CHECKPOINT_SCHEMA_VERSION = "1.0"

# 组成 runtime 身份的配置字段
RUNTIME_IDENTITY_KEYS = [
    "cwd",
    "provider",
    "model",
    "approval",
    "max_steps",
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
    }


def create_checkpoint(agent, task_state, user_message: str, trigger: str = "ask_end") -> dict:
    """创建检查点，记录当前状态。

    Args:
        agent: Agent 实例。
        task_state: TaskState 实例。
        user_message: 当前用户输入（作为 goal）。
        trigger: 触发原因（"ask_start"/"ask_end"/"manual"）。

    Returns:
        checkpoint 字典。
    """
    # 从 working memory 提取关键文件
    key_files = {}
    memory = agent.session.get("memory", {})
    working = memory.get("working", {})
    for path in working.get("recent_files", [])[-5:]:
        freshness = _file_freshness(agent._cwd, path)
        if freshness:
            key_files[path] = freshness

    checkpoint = {
        "schema_version": CHECKPOINT_SCHEMA_VERSION,
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
    }

    # 存入 session
    agent.session.setdefault("checkpoints", []).append(checkpoint)
    return checkpoint


def evaluate_resume_state(agent) -> dict:
    """评估 resume 状态。返回 status + stale_files + identity_diff + last_checkpoint。"""
    checkpoints = agent.session.get("checkpoints", [])
    if not checkpoints:
        return _resume_result("no-checkpoint")

    last = checkpoints[-1]

    # Schema 版本检查
    if last.get("schema_version") != CHECKPOINT_SCHEMA_VERSION:
        return _resume_result("schema-mismatch", last)

    result = _resume_result("full-valid", last)

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

    # 判定状态
    if result["stale_files"] or result["identity_diff"]:
        result["status"] = "workspace-mismatch" if result["identity_diff"] else "partial-stale"

    return result


def _resume_result(status: str, checkpoint: dict | None = None) -> dict:
    return {
        "status": status,
        "stale_files": [],
        "identity_diff": [],
        "last_checkpoint": checkpoint,
    }


def _file_freshness(root: str, path: str) -> str:
    """计算文件 freshness hash（mtime + size 的 SHA256）。"""
    try:
        p = Path(root) / path
        if p.exists():
            stat = p.stat()
            raw = f"{stat.st_mtime}:{stat.st_size}"
            return hashlib.sha256(raw.encode()).hexdigest()[:16]
    except OSError:
        pass
    return ""
