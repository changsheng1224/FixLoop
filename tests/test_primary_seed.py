"""Primary edit seeds come from public suspects and plans."""

from unittest.mock import MagicMock

from src.orchestrator import Orchestrator
from src.repair.execution.edit_lock import EditLockState
from src.state import RepairState, SuspectLocation


def test_primary_seed_uses_public_suspects_and_dedupes(monkeypatch, tmp_path):
    (tmp_path / "impl.py").write_text("value = 1\n", encoding="utf-8")
    orch = Orchestrator(None)
    orch._repo_root = str(tmp_path)
    orch._progress = MagicMock()

    def seed(state, root, **kwargs):
        state.suspect_locations = [
            SuspectLocation(file_path="impl.py", start_line=1, end_line=1, reason="issue 路径")
        ] * 2
        assert "test_patch" not in kwargs
        return state.suspect_locations

    monkeypatch.setattr("src.repair.localization.localize_fastpath.seed_rule_first_suspects", seed)
    state = RepairState(issue_input="public issue")
    orch._seed_patcher_primary(state)
    assert state.control.allowed_edit == ["impl.py"]
    assert not any("f2p" in key for key in state.node_timings)


def test_expand_lock_requires_read_in_new_generation(tmp_path):
    target = tmp_path / "pkg" / "impl.py"
    target.parent.mkdir(parents=True)
    target.write_text("value = 1\n", encoding="utf-8")
    lock = EditLockState(repo_root=tmp_path)
    assert lock.mark_read("pkg/impl.py")
    assert lock.expand_lock("pkg/impl.py") == (True, "expanded:pkg/impl.py")
    ok, reason = lock.check_write("pkg/impl.py")
    assert not ok and "required=1" in reason
    assert lock.mark_read("pkg/impl.py")
    assert lock.check_write("pkg/impl.py") == (True, "")
