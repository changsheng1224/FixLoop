"""Safe, ordered Turn events and an idempotent display-only projection."""

from __future__ import annotations

import threading
from copy import deepcopy


class TurnProgress:
    def __init__(self):
        self.turns: dict[str, dict] = {}
        self.incomplete = False

    def apply(self, event: dict):
        try:
            turn_id = event["turn_id"]
            seq = event["event_seq"]
            kind = event["event"]
            if (
                not isinstance(turn_id, str)
                or not turn_id
                or type(seq) is not int
                or seq < 1
                or not isinstance(kind, str)
                or not isinstance(event.get("batch_id", ""), str)
                or not isinstance(event.get("call_id", ""), str)
            ):
                raise ValueError("invalid event identity")
        except (KeyError, ValueError, TypeError):
            self.incomplete = True
            return
        turn = self.turns.setdefault(
            turn_id, {"calls": {}, "batches": {}, "event_seq": 0, "phase": ""}
        )
        # Sequence deduplication is per entity: a late event for another call
        # must still be applied even when a newer call has already completed.
        call_id = event.get("call_id")
        if call_id:
            key = (event.get("batch_id", ""), call_id)
            target = turn["calls"].setdefault(
                key, {"event_seq": 0, "batch_id": key[0], "call_id": call_id}
            )
        elif event.get("batch_id") and kind.startswith("tool_batch_"):
            target = turn["batches"].setdefault(event["batch_id"], {"event_seq": 0})
        else:
            target = turn
        # Identity descriptors can arrive late without reverting state.
        for key in ("tool_name", "ordinal"):
            if key in event:
                target.setdefault(key, event[key])
        if seq <= target["event_seq"]:
            return
        if call_id:
            target["reason"] = event.get("reason", "") if event.get("status") == "queued" else ""
            target["error_code"] = event.get("error_code", "")
            target["duration_ms"] = event.get("duration_ms", 0)
        target.update(
            {
                k: event[k]
                for k in ("status", "tool_name", "ordinal", "error_code", "duration_ms")
                if k in event
            }
        )
        target["event_seq"] = seq
        if target is turn:
            if "phase" in event:
                turn["phase"] = event["phase"]
            if kind == "turn_completed":
                turn["completed"] = True

    def snapshot(self) -> dict:
        return {
            "turns": {
                key: {
                    **{k: v for k, v in turn.items() if k not in {"calls", "batches"}},
                    "calls": [turn["calls"][key] for key in sorted(turn["calls"])],
                    "batches": {key: turn["batches"][key] for key in sorted(turn["batches"])},
                }
                for key, turn in self.turns.items()
            },
            "progress_replay_incomplete": self.incomplete,
        }


class TurnEventEmitter:
    def __init__(self, run_id: str, turn_id: str, append, callback=None):
        self.run_id = run_id
        self.turn_id = turn_id
        self.append = append
        self.callback = callback
        self.seq = 0
        self.phase = "reasoning"
        self.events: list[dict] = []
        self.projection = TurnProgress()
        self._lock = threading.RLock()

    def emit(self, kind: str, **fields):
        # Only runtime-generated status fields cross this boundary. Never
        # include arguments, model text or tool output in progress events.
        with self._lock:
            self.seq += 1
            event = {
                "event": kind,
                "run_id": self.run_id,
                "turn_id": self.turn_id,
                "event_seq": self.seq,
                "phase": self.phase,
                **fields,
            }
            self.append(kind, deepcopy(event))
            self.events.append(event)
            self.projection.apply(event)
            if self.callback is not None:
                self.callback(deepcopy(event))
            return event

    def checkpoint(self, batch=None) -> dict:
        with self._lock:
            return {
                "run_id": self.run_id,
                "turn_id": self.turn_id,
                "batch_id": batch.batch_id if batch else "",
                "event_seq": self.seq,
                "events": deepcopy(self.events),
                "calls": [call.checkpoint() for call in batch.calls] if batch else [],
            }


def replay_progress(events, *, expected_seq: int | None = None) -> dict:
    progress = TurnProgress()
    valid = []
    for raw in events or []:
        if not isinstance(raw, dict):
            progress.incomplete = True
            continue
        event = raw.get("payload", raw)
        if isinstance(event, dict):
            progress.apply(event)
            valid.append(event)
        else:
            progress.incomplete = True
    domains = {}
    for event in valid:
        turn_id = event.get("turn_id")
        seq = event.get("event_seq")
        if isinstance(turn_id, str) and type(seq) is int and seq > 0:
            domains.setdefault(turn_id, set()).add(seq)
    if not domains:
        progress.incomplete = True
    for sequences in domains.values():
        end = expected_seq if expected_seq is not None else max(sequences)
        if type(end) is not int or end < 1 or len(sequences) != end or max(sequences) != end:
            progress.incomplete = True
    return progress.snapshot()


def restore_progress(checkpoint: dict, *, operations=()) -> dict:
    """Overlay trusted receipts on display state; never authorize replay.

    A trace can end before its completed event even though the Plan journal
    already committed the result. Journal receipts take precedence for display.
    No synthetic event sequence or resumable execution journal is created here.
    """
    if not isinstance(checkpoint, dict):
        return {"turns": {}, "progress_replay_incomplete": True}
    display = replay_progress(checkpoint.get("events"), expected_seq=checkpoint.get("event_seq"))
    batch_id = checkpoint.get("batch_id", "")
    if not isinstance(batch_id, str):
        batch_id = ""
        display["progress_replay_incomplete"] = True
    turn_id = checkpoint.get("turn_id", "")
    if not isinstance(turn_id, str) or not turn_id:
        display["progress_replay_incomplete"] = True
        return display
    turn = display["turns"].setdefault(turn_id, {"calls": [], "batches": {}, "phase": ""})
    calls = {(call.get("batch_id", ""), call.get("call_id", "")): call for call in turn["calls"]}
    saved_calls = checkpoint.get("calls") or []
    if not isinstance(saved_calls, list):
        saved_calls = []
        display["progress_replay_incomplete"] = True
    for saved in saved_calls:
        if not isinstance(saved, dict) or not isinstance(saved.get("call_id"), str):
            display["progress_replay_incomplete"] = True
            continue
        key = (batch_id, saved.get("call_id", ""))
        call = calls.setdefault(key, {**saved, "batch_id": key[0], "confirmation": "unconfirmed"})
        receipt = saved.get("receipt")
        if (
            isinstance(receipt, dict)
            and receipt.get("call_id") == key[1]
            and receipt.get("run_id") == checkpoint.get("run_id")
        ):
            call.update(
                {
                    "confirmation": "receipt",
                    "receipt_ref": receipt.get("receipt_id", ""),
                    "result_ref": saved.get("result_ref", ""),
                    "status": "succeeded"
                    if receipt.get("status") == "success"
                    else receipt.get("status", call.get("status", "uncertain")),
                }
            )
    for operation in operations:
        if operation.get("turn_id") != turn_id:
            continue
        key = (operation.get("batch_id", ""), operation.get("call_id", ""))
        call = calls.setdefault(key, {"batch_id": key[0], "call_id": key[1]})
        call.setdefault("tool_name", operation.get("tool", ""))
        if "ordinal" in operation:
            call.setdefault("ordinal", operation["ordinal"])
        receipt = operation.get("receipt") or {}
        if not isinstance(receipt, dict):
            receipt = {}
            display["progress_replay_incomplete"] = True
        confirmed = (
            operation.get("phase") == "result_recorded"
            and receipt.get("call_id") == key[1]
            and receipt.get("run_id") == checkpoint.get("run_id")
            and receipt.get("status") == operation.get("status")
        )
        if confirmed:
            call.update(
                {
                    "status": "succeeded"
                    if receipt["status"] == "success" and operation.get("execution_stopped")
                    else "uncertain"
                    if receipt["status"] == "uncertain" or not operation.get("execution_stopped")
                    else "failed",
                    "confirmation": "plan_receipt",
                    "receipt_ref": receipt.get("receipt_id", ""),
                    "result_ref": operation.get("observation_id", ""),
                }
            )
        else:
            call.update({"status": "uncertain", "confirmation": "unconfirmed"})
    for call in calls.values():
        call.setdefault("confirmation", "receipt" if call.get("receipt") else "unconfirmed")
    turn["calls"] = [calls[key] for key in sorted(calls)]
    return display
