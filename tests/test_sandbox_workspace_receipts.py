"""Recovery evidence remains inspectable without authorizing another call."""

import pytest

from agent_runtime.linux_sandbox.receipts import ReceiptStore


def test_long_path_receipts_are_not_treated_as_absent(tmp_path):
    from agent_runtime.linux_sandbox.receipts import _read, _write
    from agent_runtime.state_root import native_io_path

    path = tmp_path / ("a" * 90) / ("b" * 90) / "state.json"
    native_io_path(path.parent).mkdir(parents=True)
    _write(path, {"pending": "approved"})
    assert _read(path) == {"pending": "approved"}
    _write(path, {"pending": "consumed"})
    assert _read(path) == {"pending": "consumed"}


def test_receipt_access_errors_do_not_mean_missing_state(tmp_path, monkeypatch):
    from pathlib import Path

    from agent_runtime.linux_sandbox.receipts import _read

    def denied(*args, **kwargs):
        raise PermissionError("blocked")

    monkeypatch.setattr(Path, "read_text", denied)
    with pytest.raises(PermissionError):
        _read(tmp_path / "state.json")


@pytest.mark.parametrize("cleanup", ["confirmed", "unverified"])
def test_uncertain_receipt_is_readable_but_cannot_resume(tmp_path, cleanup):
    store = ReceiptStore(tmp_path / "state", tmp_path / "workspace")
    store.transition("call", "planned", policy_digest="policy")
    store.transition("call", "running")
    store.record_mutation("call", {"path": "a.txt", "phase": "intent"})
    expected = store.transition(
        "call", "terminal", result={"execution_status": "uncertain", "cleanup": cleanup}
    )
    assert store.inspect("call") == expected
    with pytest.raises(ValueError, match="execution_uncertain"):
        store.reconcile("policy")


def test_inspect_terminal_still_checks_durable_pair(tmp_path):
    store = ReceiptStore(tmp_path / "state", tmp_path / "workspace")
    store.transition("call", "planned", policy_digest="policy")
    store.transition("call", "terminal", no_target_started=True,
                     result={"execution_status": "start_failed", "cleanup": "confirmed"})
    store.receipt_path("call").unlink()
    with pytest.raises(ValueError, match="receipt_invalid"):
        store.inspect("call")
