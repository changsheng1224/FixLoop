"""ProgressEmitter：Phase A 进度最小集。"""

from __future__ import annotations

from io import StringIO

from src.repair.progress import ProgressEmitter, ProgressEvent


def test_emit_seed_ready_writes_event_and_summary():
    sink = StringIO()
    events: list[ProgressEvent] = []
    em = ProgressEmitter(quiet=False, text_sink=sink, record=events.append)
    em.emit("seed_ready", summary="allowed_edit=2", allowed_edit=["a.py", "b.py"])

    assert events
    ev = events[0]
    assert ev.event == "seed_ready"
    assert ev.summary == "allowed_edit=2"
    assert ev.ts
    assert "seed_ready" in sink.getvalue()
    assert "allowed_edit=2" in sink.getvalue()


def test_quiet_skips_user_face_keeps_record():
    sink = StringIO()
    events: list[ProgressEvent] = []
    em = ProgressEmitter(quiet=True, text_sink=sink, record=events.append)
    em.emit("repair_started", summary="go")
    assert events[0].event == "repair_started"
    assert sink.getvalue() == ""


def test_emit_failure_does_not_raise():
    def bad(_ev: ProgressEvent) -> None:
        raise RuntimeError("boom")

    em = ProgressEmitter(quiet=False, text_sink=StringIO(), record=bad)
    em.emit("repair_finished", summary="done")  # must not raise


def test_phase_a_required_events_accepted():
    em = ProgressEmitter(quiet=True)
    for name in ("repair_started", "seed_ready", "patcher_turn", "repair_finished"):
        em.emit(name, summary=name)


def test_heartbeat_hidden_from_text_by_default():
    sink = StringIO()
    events: list[ProgressEvent] = []
    em = ProgressEmitter(
        quiet=False, text_sink=sink, record=events.append, heartbeat_to_text=False
    )
    em.emit("heartbeat", summary="alive")
    em.emit("seed_ready", summary="ok")
    assert events[0].event == "heartbeat"
    text = sink.getvalue()
    assert "heartbeat" not in text
    assert "seed_ready" in text


def test_progress_from_env_defaults_stdout(monkeypatch):
    import sys

    from src.repair.progress import progress_emitter_from_env

    monkeypatch.delenv("FIXLOOP_PROGRESS_STDOUT", raising=False)
    em = progress_emitter_from_env()
    assert em.text_sink is sys.stdout
