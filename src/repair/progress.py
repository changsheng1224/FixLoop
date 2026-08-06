"""Repair / SWE 阶段性进度输出（与 Trace 同源阶段名）。"""

from __future__ import annotations

import json
import os
import sys
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, TextIO

__all__ = [
    "KNOWN_EVENTS",
    "PHASE_A_EVENTS",
    "ProgressEmitter",
    "ProgressEvent",
    "progress_emitter_from_env",
]

PHASE_A_EVENTS = frozenset(
    {
        "repair_started",
        "seed_ready",
        "patcher_turn",
        "repair_finished",
    }
)

KNOWN_EVENTS = PHASE_A_EVENTS | frozenset(
    {
        "tool_progress",
        "quick_test",
        "critic_progress",
        "verify_progress",
        "heartbeat",
        "instance_progress",
        "critic_started",
        "critic_finished",
        "seed_span",
        "apply_patch_span",
    }
)


@dataclass
class ProgressEvent:
    event: str
    summary: str = ""
    ts: float = 0.0
    extras: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        d = {"event": self.event, "summary": self.summary, "ts": self.ts}
        d.update(self.extras)
        return d


def _default_text_sink() -> TextIO:
    """默认 stdout，避免 PowerShell ``2>&1 | Tee-Object`` 把进度当错误。"""
    raw = (os.environ.get("FIXLOOP_PROGRESS_STDOUT") or "1").strip().lower()
    if raw in ("0", "false", "off", "no", "stderr"):
        return sys.stderr
    return sys.stdout


def progress_emitter_from_env(
    *,
    text_sink: TextIO | None = None,
    record: Callable[[ProgressEvent], None] | None = None,
) -> ProgressEmitter:
    quiet = (os.environ.get("FIXLOOP_PROGRESS") or "1").strip().lower() in (
        "0",
        "false",
        "off",
        "quiet",
        "no",
    )
    jsonl = (os.environ.get("FIXLOOP_PROGRESS_JSONL") or "").strip() or None
    # 心跳默认只写 jsonl，减少控制台刷屏；FIXLOOP_PROGRESS_HEARTBEAT_TEXT=1 才打字面
    hb_text = (os.environ.get("FIXLOOP_PROGRESS_HEARTBEAT_TEXT") or "0").strip().lower() in (
        "1",
        "true",
        "on",
        "yes",
    )
    return ProgressEmitter(
        quiet=quiet,
        text_sink=text_sink,
        jsonl_path=jsonl,
        record=record,
        heartbeat_to_text=hb_text,
    )


class ProgressEmitter:
    """向 CLI（默认 stdout）打阶段摘要；可选 jsonl；emit 失败永不阻断主环。"""

    def __init__(
        self,
        *,
        quiet: bool = False,
        text_sink: TextIO | None = None,
        jsonl_path: str | None = None,
        record: Callable[[ProgressEvent], None] | None = None,
        heartbeat_to_text: bool = False,
    ) -> None:
        self.quiet = quiet
        self.text_sink = text_sink if text_sink is not None else _default_text_sink()
        self.jsonl_path = jsonl_path
        self._record = record
        self.heartbeat_to_text = heartbeat_to_text
        self._hb_stop: threading.Event | None = None
        self._hb_thread: threading.Thread | None = None

    def emit(self, event: str, summary: str = "", **extras: Any) -> ProgressEvent | None:
        try:
            ev = ProgressEvent(
                event=str(event or ""),
                summary=str(summary or ""),
                ts=time.time(),
                extras=dict(extras),
            )
            if self._record is not None:
                self._record(ev)
            skip_text = ev.event == "heartbeat" and not self.heartbeat_to_text
            if not self.quiet and not skip_text:
                line = f"[progress] {ev.event}"
                if ev.summary:
                    line += f": {ev.summary}"
                print(line, file=self.text_sink, flush=True)
            if self.jsonl_path:
                with open(self.jsonl_path, "a", encoding="utf-8") as f:
                    f.write(json.dumps(ev.to_dict(), ensure_ascii=False) + "\n")
            return ev
        except Exception:
            return None

    def start_heartbeat(self, interval_s: float = 60.0, summary: str = "alive") -> None:
        """可选后台心跳（Phase C）；失败不影响主环。默认间隔 60s。"""
        try:
            if self._hb_thread is not None:
                return
            stop = threading.Event()
            self._hb_stop = stop

            def _run() -> None:
                while not stop.wait(interval_s):
                    self.emit("heartbeat", summary=summary)

            th = threading.Thread(target=_run, name="fixloop-progress-hb", daemon=True)
            self._hb_thread = th
            th.start()
        except Exception:
            pass

    def stop_heartbeat(self) -> None:
        try:
            if self._hb_stop is not None:
                self._hb_stop.set()
            self._hb_thread = None
            self._hb_stop = None
        except Exception:
            pass
