"""L2 CLI repair 退出码常量、状态映射与启动前配置预检。"""

from __future__ import annotations

import os
from pathlib import Path

from src.repair.verification.termination import has_actionable_patch, resolve_terminal_status
from src.state import RepairState, RepairStatus

REPAIR_EXIT_OK = 0
REPAIR_EXIT_FAIL = 1
REPAIR_EXIT_CONFIG = 2
REPAIR_EXIT_TIMEOUT = 3
# 沙箱审批暂停：运行未失败，等待调用方履行审批后从同一 checkpoint 续跑。
REPAIR_EXIT_APPROVAL = 4

__all__ = [
    "REPAIR_EXIT_OK",
    "REPAIR_EXIT_FAIL",
    "REPAIR_EXIT_CONFIG",
    "REPAIR_EXIT_TIMEOUT",
    "REPAIR_EXIT_APPROVAL",
    "repair_config_error",
    "repair_exit_code",
]


def repair_config_error(repo: str, *, api_key: str | None = None) -> str | None:
    """启动前配置检查；无错误返回 None。"""
    if not Path(repo).exists():
        return f"错误: --repo 不存在: {repo}"
    key = api_key if api_key is not None else os.environ.get("DEEPSEEK_API_KEY", "")
    if not str(key).strip():
        return "错误: 未设置 DEEPSEEK_API_KEY"
    return None


def repair_exit_code(state: RepairState) -> int:
    """根据 RepairState 计算 repair 子命令进程退出码。"""
    terminal = resolve_terminal_status(state)
    if terminal == RepairStatus.TIMEOUT:
        return REPAIR_EXIT_TIMEOUT
    if terminal == RepairStatus.WAITING_APPROVAL:
        return REPAIR_EXIT_APPROVAL
    if has_actionable_patch(state):
        return REPAIR_EXIT_OK
    return REPAIR_EXIT_FAIL
