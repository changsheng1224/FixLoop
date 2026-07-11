"""Blackboard 读写与 trace mixin（从 pipeline 抽出）。"""

from __future__ import annotations

from src.blackboard import Blackboard
from src.repair.blackboard_merge import (
    BLACKBOARD_SCHEMA_VERSION,
    merge_blackboard_for_patch,
    write_feedback_to_blackboard,
    write_localize_phase_to_blackboard,
)
from src.repair.run_context import RepairRunContext
from src.state import RepairState, RetrievedContext, SuspectLocation

__all__ = ["BlackboardMixin"]


class BlackboardMixin:
    """Orchestrator Blackboard 代理与 trace。"""

    _repair_ctx: RepairRunContext | None

    def _active_repair_ctx(self) -> RepairRunContext:
        ctx = getattr(self, "_repair_ctx", None)
        if ctx is None:
            raise RuntimeError("repair run context is not active")
        return ctx

    def _emit_bb_trace(self, event: str, payload: dict) -> None:
        tracer = self._active_repair_ctx().repair_tracer
        if tracer is not None:
            tracer.emit("orchestrator", event, payload)

    def _init_repair_blackboard(self) -> None:
        self._active_repair_ctx().blackboard = Blackboard()

    def _write_localize_phase_to_blackboard(
        self,
        state: RepairState,
        suspects: list[SuspectLocation],
        context: RetrievedContext | None,
    ) -> dict:
        """Write localize/retrieve outputs to Blackboard (merge deferred to patch)."""
        bb = self._active_repair_ctx().blackboard
        if bb is None:
            state.suspect_locations = suspects
            state.retrieved_context = context or RetrievedContext()
            return {"suspects_written": len(suspects), "context_keys_written": 0}

        write_stats = write_localize_phase_to_blackboard(bb, suspects, context)
        self._emit_bb_trace("blackboard_written", {**write_stats, "phase": "localize"})
        self._emit_bb_trace("blackboard_snapshot", bb.snapshot())
        return write_stats

    def _merge_blackboard_for_patch(self, state: RepairState) -> dict:
        """Read Blackboard at patch boundary and materialize into RepairState."""
        bb = self._active_repair_ctx().blackboard
        if bb is None:
            return {}

        merge_meta = merge_blackboard_for_patch(state, bb)
        self._emit_bb_trace(
            "blackboard_merge_for_patch",
            {
                "suspect_count": merge_meta["suspect_count"],
                "context_keys": merge_meta["context_keys"],
                "conflict_count": len(merge_meta["conflicts"]),
                "conflicts_resolved": merge_meta["conflicts_resolved"],
                "scratch_feedback_applied": merge_meta["scratch_feedback_applied"],
                "retry_count": merge_meta["retry_count"],
                "blackboard_schema_version": BLACKBOARD_SCHEMA_VERSION,
            },
        )
        if merge_meta["conflicts"]:
            self._emit_bb_trace(
                "blackboard_conflicts",
                {"conflicts": merge_meta["conflicts"]},
            )
        self._emit_bb_trace("blackboard_snapshot", merge_meta["snapshot"])
        return merge_meta

    def _write_feedback_to_blackboard(self, feedback: str) -> None:
        bb = self._active_repair_ctx().blackboard
        if bb is not None:
            write_feedback_to_blackboard(bb, feedback)
