"""Repair 墙钟超时：cancel → 短 grace → salvage → 非阻塞 shutdown。"""

from __future__ import annotations

import time
from typing import Any

from agent_runtime.logging_setup import get_logger
from src.repair.verification.termination import RepairTerminalStatus, finalize_repair_state

__all__ = [
    "GRACE_AFTER_TIMEOUT_S",
    "MAX_WALL_OVERSHOOT_S",
    "handle_repair_wall_timeout",
]

log = get_logger("repair.timeout")

# 超时后给 worker 协作退出的宽限；总目标墙钟 ≤ timeout + MAX_WALL_OVERSHOOT_S
GRACE_AFTER_TIMEOUT_S = 2
MAX_WALL_OVERSHOOT_S = 30


def handle_repair_wall_timeout(
    orch: Any,
    state: Any,
    *,
    initial_snapshot: dict,
    repair_timeout_s: int,
    cancel_token: Any,
    wall_started: float | None = None,
    grace_s: float = GRACE_AFTER_TIMEOUT_S,
) -> Any:
    """FuturesTimeout 后的硬停路径：先 salvage，再必要时 restore。

    不在此函数内 join worker；调用方应对 ThreadPoolExecutor ``shutdown(wait=False)``。
    """
    t0 = wall_started if wall_started is not None else time.monotonic()
    if cancel_token is not None:
        try:
            cancel_token.cancel("timeout")
        except Exception:
            pass

    # 短 grace：让协作式 cancel 有机会停下写盘
    grace = max(0.0, min(float(grace_s), float(MAX_WALL_OVERSHOOT_S)))
    if grace > 0:
        time.sleep(grace)

    keep_patches = bool(getattr(state, "candidate_patches", None))
    salvaged: list = []
    if not keep_patches:
        try:
            salvaged = orch._salvage_patches_from_disk(state, initial_snapshot) or []
        except Exception as e:
            log.warning("[timeout] salvage failed: %s", e)
            salvaged = []
        if salvaged:
            state.candidate_patches = salvaged
            state.node_timings["repair_timeout_salvaged"] = len(salvaged)
            state.node_timings["phase_timeout_salvaged"] = len(salvaged)
            keep_patches = True

    if keep_patches:
        state.node_timings["phase_timeout_kept_patches"] = True
        # 保留已登记/salvage 的 diff；不整仓回滚（避免抹掉 model_patch）
    else:
        try:
            orch._restore_repo_snapshot(initial_snapshot)
        except Exception as e:
            log.warning("[timeout] restore failed: %s", e)

    state.status = RepairTerminalStatus.TIMEOUT
    state.agent_errors["orchestrator"] = f"repair timeout ({repair_timeout_s}s)"
    state.node_timings["repair_timeout"] = repair_timeout_s
    # overshoot：相对「timeout 触发时刻」的额外耗时（grace + salvage），目标 ≤ MAX_WALL_OVERSHOOT_S
    overshoot = max(0.0, time.monotonic() - t0)
    if wall_started is not None:
        # 全流程墙钟超出 timeout 的部分
        overshoot = max(0.0, time.monotonic() - wall_started - float(repair_timeout_s))
    state.node_timings["repair_wall_overshoot_s"] = round(overshoot, 3)
    state.node_timings["repair_timeout_grace_s"] = grace

    try:
        if hasattr(orch, "_progress_emitter"):
            orch._progress_emitter().emit(
                "repair_finished",
                summary=(
                    f"status=timeout salvaged={len(state.candidate_patches or [])} "
                    f"overshoot_s={state.node_timings['repair_wall_overshoot_s']}"
                ),
            )
    except Exception:
        pass

    log.warning(
        "修复超时硬停 (%ds)，salvaged=%s overshoot_s=%.2f",
        repair_timeout_s,
        len(state.candidate_patches or []),
        state.node_timings["repair_wall_overshoot_s"],
    )
    finalize_repair_state(state)
    return state
