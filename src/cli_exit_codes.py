"""L2 CLI repair 退出码常量、状态映射与启动前配置预检。"""

from __future__ import annotations

import os
from pathlib import Path

from src.repair.verification.termination import has_repair_timeout, is_repair_success
from src.state import RepairState

REPAIR_EXIT_OK = 0
REPAIR_EXIT_FAIL = 1
REPAIR_EXIT_CONFIG = 2
REPAIR_EXIT_TIMEOUT = 3

__all__ = [
    "REPAIR_EXIT_OK",
    "REPAIR_EXIT_FAIL",
    "REPAIR_EXIT_CONFIG",
    "REPAIR_EXIT_TIMEOUT",
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
    if has_repair_timeout(state):
        return REPAIR_EXIT_TIMEOUT
    if is_repair_success(state):
        return REPAIR_EXIT_OK
    return REPAIR_EXIT_FAIL
