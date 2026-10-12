"""Filesystem-path SQLite regression for the Windows production runtime."""

import os
from pathlib import Path

import pytest

from agent_runtime.plan_runtime.journal import PlanStore
from agent_runtime.state_root import native_io_path


@pytest.mark.skipif(os.name != "nt", reason="Windows SQLite MAX_PATH regression")
def test_long_path_plan_journal_roundtrip(tmp_path):
    root = tmp_path / ("d" * (246 - len(str(tmp_path.resolve())) - 1))
    assert len(str(root.resolve() / "journal.sqlite3")) > 260
    identity = {"task_id": "long-path", "run_id": "run", "workspace_id": "workspace"}
    with_store = PlanStore(root, identity)
    try:
        with_store.append("proof", {"value": 42})
        key = with_store.put_blob("deep path proof")
    finally:
        with_store.close()
    reopened = PlanStore(root, identity)
    try:
        assert reopened.events()[-1]["payload"] == {"value": 42}
        assert reopened.get_blob(key) == "deep path proof"
    finally:
        reopened.close()


def test_native_database_path_preserves_platform_semantics():
    value = "relative/database.sqlite3"
    assert native_io_path(value) == Path(value)
    assert native_io_path(Path(value).absolute()) == Path(value).absolute()


@pytest.mark.skipif(os.name != "nt", reason="Windows UNC database spelling")
def test_unc_and_already_extended_database_paths():
    short = Path(r"\\server\share\data.sqlite3")
    assert native_io_path(short) == short
    long = short.parent / ("d" * 120) / ("e" * 120) / "data.sqlite3"
    assert str(native_io_path(long)) == "\\\\?\\UNC\\" + str(long)[2:]
    extended = r"\\?\C:\state\data.sqlite3"
    assert str(native_io_path(extended)) == extended


@pytest.mark.skipif(os.name != "nt", reason="Windows path identity")
def test_plan_store_keeps_logical_root(tmp_path):
    with_store = PlanStore(tmp_path, {"run_id": "canonical"})
    try:
        assert with_store.root == tmp_path
        assert not str(with_store.root).startswith("\\\\?\\")
    finally:
        with_store.close()


@pytest.mark.skipif(os.name != "nt", reason="Windows extended path normalization")
@pytest.mark.parametrize("name", ["ambiguous.", "CON", "nul.txt", "COM1.log", "LPT9", "COM¹.log"])
def test_long_path_conversion_refuses_changed_win32_names(name):
    value = Path("C:/") / ("d" * 120) / ("e" * 120) / name
    with pytest.raises(ValueError, match="ambiguous Windows path"):
        native_io_path(value)


@pytest.mark.skipif(os.name != "nt", reason="Windows observation blob MAX_PATH regression")
def test_long_path_observation_stays_durable(tmp_path):
    from agent_runtime.context_runtime import ObservationStore

    workspace = tmp_path / "project"
    workspace.mkdir()
    state_root = tmp_path / ("s" * (220 - len(str(tmp_path.resolve())) - 1))
    state = {"id": "deep", "session_scope": {"session_id": "deep"}}
    store = ObservationStore(state, str(workspace), str(state_root))
    try:
        observation = store.put("list_files", {"path": "."}, "v.py\ntest_value.py")
        assert not observation.raw_ref.startswith("memory:")
        assert store.expand(observation.observation_id) == "v.py\ntest_value.py"
    finally:
        store.close()
    reopened = ObservationStore(
        {"id": "deep", "session_scope": {"session_id": "deep"}}, str(workspace), str(state_root)
    )
    try:
        assert reopened.expand(observation.observation_id) == "v.py\ntest_value.py"
        assert reopened.gc(max_records=0)["removed"] == 1
        assert not native_io_path(observation.raw_ref).exists()
    finally:
        reopened.close()
