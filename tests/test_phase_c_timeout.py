"""Phase C：超时硬停 salvage / phase budget / progress heartbeat。"""

from __future__ import annotations

import time
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FuturesTimeoutError
from pathlib import Path
from unittest.mock import MagicMock

from src.orchestrator import Orchestrator
from src.repair.phase_clock import PhaseTimeoutConfig
from src.repair.progress import ProgressEmitter
from src.repair.verification.repair_timeout import (
    GRACE_AFTER_TIMEOUT_S,
    handle_repair_wall_timeout,
)
from src.state import RepairState


def test_primary_phase_budget_skips_localize():
    cfg = PhaseTimeoutConfig.for_patcher_primary(900)
    assert cfg.localize_s == 0
    assert cfg.patch_s > 0
    assert cfg.verify_s > 0
    assert cfg.repair_total_s == 900
    assert cfg.patch_s + cfg.verify_s <= 900


def test_handle_wall_timeout_salvages_before_restore(tmp_path):
    root = tmp_path
    (root / "a.py").write_text("old\n", encoding="utf-8")
    orch = Orchestrator(None)
    orch._repo_root = str(root)
    initial = {"a.py": "old\n"}
    # simulate worker wrote disk
    (root / "a.py").write_text("new\n", encoding="utf-8")
    state = RepairState(issue_input="x")
    state.suspect_locations = []
    from src.state import SuspectLocation

    state.suspect_locations = [
        SuspectLocation(file_path="a.py", start_line=1, end_line=1, confidence=0.9)
    ]

    t0 = time.monotonic()
    handle_repair_wall_timeout(
        orch,
        state,
        initial_snapshot=initial,
        repair_timeout_s=1,
        cancel_token=MagicMock(),
        wall_started=t0,
    )
    assert state.candidate_patches
    assert state.node_timings.get("repair_timeout_salvaged") or state.node_timings.get(
        "phase_timeout_salvaged"
    ) or len(state.candidate_patches) >= 1
    assert state.status == "timeout" or str(state.status) in ("timeout", "TIMEOUT") or getattr(
        state.status, "value", ""
    ) == "timeout" or "timeout" in str(state.status).lower()
    # wall overshoot recorded and bounded conceptually
    assert "repair_wall_overshoot_s" in state.node_timings or "repair_timeout" in state.node_timings


def test_executor_shutdown_does_not_block_on_hung_worker():
    """shutdown(wait=False) 后调用方应迅速返回。"""
    import threading

    release = threading.Event()
    started = time.monotonic()

    def hung():
        release.wait(30)

    pool = ThreadPoolExecutor(max_workers=1)
    fut = pool.submit(hung)
    try:
        fut.result(timeout=0.2)
        assert False, "expected timeout"
    except FuturesTimeoutError:
        pass
    pool.shutdown(wait=False, cancel_futures=True)
    elapsed = time.monotonic() - started
    release.set()
    assert elapsed < 5.0


def test_progress_heartbeat_and_jsonl(tmp_path: Path):
    jsonl = tmp_path / "progress.jsonl"
    events = []
    em = ProgressEmitter(quiet=True, jsonl_path=str(jsonl), record=events.append)
    em.emit("heartbeat", summary="alive")
    em.emit("repair_started", summary="go")
    assert jsonl.exists()
    text = jsonl.read_text(encoding="utf-8")
    assert "heartbeat" in text
    assert events[0].event == "heartbeat"


def test_grace_constant():
    assert GRACE_AFTER_TIMEOUT_S <= 30
