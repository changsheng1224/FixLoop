"""单次 repair() 调用的 ephemeral 运行时上下文。"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from src.blackboard import Blackboard
from src.repair.phase_clock import PhaseTimeoutConfig

__all__ = ["RepairRunContext"]


@dataclass
class RepairRunContext:
    """Orchestrator repair 期间的可变会话状态（finally 中清理）。"""

    phase_timeout_config: PhaseTimeoutConfig | None = None
    cancel_token: Any = None
    repair_started_at: float | None = None
    blackboard: Blackboard | None = None
    repair_tracer: Any = field(default=None, repr=False)
    log_run_id_token: Any = field(default=None, repr=False)
    worktree_handle: Any = field(default=None, repr=False)
    worktree_original_roots: dict = field(default_factory=dict, repr=False)
    original_repo_root: str = ""
    worktree_initial_snapshot: dict = field(default_factory=dict, repr=False)
    # SWE-bench 等：verify 前临时应用的官方 test_patch（不进 model 导出）
    verify_test_patch: str = ""
