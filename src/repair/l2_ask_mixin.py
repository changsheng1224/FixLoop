"""L2 agent_ask 生命周期 mixin（从 pipeline 抽出）。"""

from __future__ import annotations

import time

from src.repair.l2_binding import bind_l2_context, clear_l2_context, make_repair_task_id
from src.repair.run_context import RepairRunContext
from src.state import AgentAskRef, RepairState

__all__ = ["L2AskMixin"]


class L2AskMixin:
    """Orchestrator L2 ask 绑定与 trace。"""

    _repair_ctx: RepairRunContext | None

    def _active_repair_ctx(self) -> RepairRunContext:
        ctx = getattr(self, "_repair_ctx", None)
        if ctx is None:
            raise RuntimeError("repair run context is not active")
        return ctx

    def _l2_elapsed_ms(self) -> int:
        started = self._active_repair_ctx().repair_started_at
        if started is None:
            return 0
        return int((time.time() - started) * 1000)

    def _begin_l2_agent_ask(
        self,
        state: RepairState,
        agent,
        *,
        agent_name: str,
        phase: str,
        attempt: int,
    ) -> str:
        repair_run_id = state.repair_run_id
        if not repair_run_id or agent is None:
            return ""
        started_ms = self._l2_elapsed_ms()
        task_id = bind_l2_context(
            agent,
            repair_run_id=repair_run_id,
            agent_name=agent_name,
            phase=phase,
            attempt=attempt,
            started_ms=started_ms,
        )
        tracer = self._active_repair_ctx().repair_tracer
        if tracer is not None:
            tracer.emit(
                agent_name,
                "agent_ask_started",
                {
                    "task_id": task_id,
                    "repair_run_id": repair_run_id,
                    "l2_agent": agent_name,
                    "l2_phase": phase,
                    "l2_attempt": attempt,
                    "started_ms": started_ms,
                },
            )
        return task_id

    def _finish_l2_agent_ask(
        self,
        state: RepairState,
        agent,
        *,
        agent_name: str,
        phase: str,
        attempt: int,
        task_id: str,
        elapsed_ms: int,
        stop_reason: str = "",
        tool_steps: int = 0,
    ) -> None:
        if not task_id or not state.repair_run_id:
            clear_l2_context(agent)
            return
        finished_ms = self._l2_elapsed_ms()
        started_ms = int(getattr(agent, "_l2_ask_started_ms", finished_ms - elapsed_ms))
        ref = AgentAskRef(
            agent=agent_name,
            phase=phase,
            attempt=int(attempt),
            task_id=task_id,
            run_id=state.repair_run_id,
            started_ms=started_ms,
            finished_ms=finished_ms,
            stop_reason=stop_reason,
            tool_steps=int(tool_steps),
        )
        state.agent_asks.append(ref)
        tracer = self._active_repair_ctx().repair_tracer
        if tracer is not None:
            tracer.emit(
                agent_name,
                "agent_ask_finished",
                {
                    **ref.to_dict(),
                    "elapsed_ms": elapsed_ms,
                },
            )
        clear_l2_context(agent)

    def _record_l2_synthetic_ask(
        self,
        state: RepairState,
        *,
        agent_name: str,
        phase: str,
        attempt: int,
        elapsed_ms: int,
        stop_reason: str = "",
        tool_steps: int = 0,
    ) -> str:
        """Patcher complete_once / Verifier 等非 AgentLoop 路径。"""
        repair_run_id = state.repair_run_id
        if not repair_run_id:
            return ""
        task_id = make_repair_task_id(repair_run_id, agent_name, attempt)
        finished_ms = self._l2_elapsed_ms()
        started_ms = max(0, finished_ms - int(elapsed_ms))
        ref = AgentAskRef(
            agent=agent_name,
            phase=phase,
            attempt=int(attempt),
            task_id=task_id,
            run_id=repair_run_id,
            started_ms=started_ms,
            finished_ms=finished_ms,
            stop_reason=stop_reason,
            tool_steps=int(tool_steps),
        )
        tracer = self._active_repair_ctx().repair_tracer
        if tracer is not None:
            payload = {
                "task_id": task_id,
                "repair_run_id": repair_run_id,
                "l2_agent": agent_name,
                "l2_phase": phase,
                "l2_attempt": attempt,
                "started_ms": started_ms,
                "synthetic": True,
            }
            tracer.emit(agent_name, "agent_ask_started", payload)
            tracer.emit(
                agent_name,
                "agent_ask_finished",
                {**ref.to_dict(), "elapsed_ms": elapsed_ms, "synthetic": True},
            )
        state.agent_asks.append(ref)
        return task_id
