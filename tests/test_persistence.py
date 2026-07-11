"""TaskState + SessionStore + RunStore 单测。"""

import json
import tempfile

import pytest

from agent_runtime.run_ids import is_valid_run_id
from agent_runtime.run_store import RunStore
from agent_runtime.session_store import SessionStore
from agent_runtime.task_state import TaskState


class TestTaskState:
    """TaskState 状态机测试。"""

    def test_create_defaults_to_running(self):
        ts = TaskState.create(user_request="test")
        assert ts.status == "running"
        assert ts.user_request == "test"
        assert is_valid_run_id(ts.run_id)

    def test_state_machine(self):
        ts = TaskState.create(user_request="fix bug")

        # 模拟一次 ask() 生命周期
        ts.record_attempt()
        ts.record_tool("read_file")
        assert ts.tool_steps == 1
        assert ts.attempts == 1
        assert ts.last_tool == "read_file"

        ts.record_attempt()
        ts.finish_success("问题已修复")
        assert ts.status == "completed"
        assert ts.stop_reason == "final"
        assert ts.final_answer == "问题已修复"

    def test_stop_step_limit(self):
        ts = TaskState.create(user_request="test")
        ts.stop_step_limit(6)
        assert ts.status == "stopped"
        assert ts.stop_reason == "step_limit"
        assert ts.node_timings["stop_reason_detail"] == "tool_steps > 6"

    def test_stop_retry_limit(self):
        ts = TaskState.create(user_request="test")
        ts.stop_retry_limit(22)
        assert ts.status == "failed"
        assert ts.stop_reason == "parse_fail"
        assert "attempts" in ts.node_timings["stop_reason_detail"]

    def test_to_dict_from_dict_roundtrip(self):
        ts = TaskState.create(task_id="T1", user_request="hello")
        ts.record_tool("read_file")
        ts.finish_success("done")

        data = ts.to_dict()
        restored = TaskState.from_dict(data)

        assert restored.run_id == ts.run_id
        assert restored.tool_steps == ts.tool_steps
        assert restored.status == "completed"
        assert restored.final_answer == "done"


class TestSessionStore:
    """SessionStore 读写测试。"""

    @pytest.fixture
    def store(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            yield SessionStore(root=tmpdir)

    def test_save_and_load(self, store):
        session = {"id": "s1", "history": [], "memory": {}}
        store.save(session)
        loaded = store.load("s1")
        assert loaded is not None
        assert loaded["id"] == "s1"

    def test_load_nonexistent(self, store):
        assert store.load("ghost") is None

    def test_latest(self, store):
        store.save({"id": "a"})
        store.save({"id": "b"})
        assert store.latest() == "b"

    def test_latest_empty(self, store):
        assert store.latest() is None


class TestRunStore:
    """RunStore 工件写入测试。"""

    @pytest.fixture
    def store(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            yield RunStore(root=tmpdir)

    @pytest.fixture
    def task_state(self):
        ts = TaskState.create(user_request="test")
        ts.finish_success("answer")
        return ts

    def test_write_task_state_atomic(self, store, task_state):
        path = store.write_task_state(task_state)
        assert path.exists()
        data = json.loads(path.read_text())
        assert data["status"] == "completed"
        # 确保没有 .tmp 残留
        assert not path.with_suffix(".tmp").exists()

    def test_append_trace(self, store, task_state):
        store.append_trace(task_state, "run_started")
        store.append_trace(task_state, "tool_executed", {"tool": "read_file"})

        trace_path = store.runs_dir / task_state.run_id / "trace.jsonl"
        assert trace_path.exists()
        lines = trace_path.read_text().strip().splitlines()
        assert len(lines) == 2
        record = json.loads(lines[1])
        assert record["event"] == "tool_executed"
        assert record["payload"]["tool"] == "read_file"

    def test_append_trace_event(self, store):
        store.append_trace_event("repair-test-001", "repair_started", {"agent": "orchestrator"})
        trace_path = store.runs_dir / "repair-test-001" / "trace.jsonl"
        assert trace_path.exists()
        record = json.loads(trace_path.read_text().strip())
        assert record["event"] == "repair_started"
        assert record["payload"]["agent"] == "orchestrator"

    def test_write_report(self, store, task_state):
        report = {"total_tokens": 500, "tool_steps": 2}
        store.write_report(task_state, report)
        report_path = store.runs_dir / task_state.run_id / "report.json"
        assert report_path.exists()
        data = json.loads(report_path.read_text())
        assert data["total_tokens"] == 500


class TestTraceGzip:
    """trace.jsonl gzip 归档测试。"""

    @pytest.fixture
    def store(self, tmp_path):
        return RunStore(str(tmp_path))

    def test_read_trace_lines_plain_jsonl(self, store):
        run_id = "gzip-test-001"
        store.append_trace_event(run_id, "run_started")
        store.append_trace_event(run_id, "tool_executed", {"tool": "read_file"})
        lines = store.read_trace_lines(run_id)
        assert len(lines) == 2
        assert json.loads(lines[0])["event"] == "run_started"

    def test_read_trace_lines_missing_run(self, store):
        assert store.read_trace_lines("nonexistent") == []

    def test_no_compress_below_threshold(self, store, monkeypatch):
        monkeypatch.setenv("FIXLOOP_TRACE_GZIP_LINES", "9999")
        run_id = "gzip-test-002"
        store.append_trace_event(run_id, "run_started")
        store.append_trace_event(run_id, "tool_executed", {"tool": "read_file"})
        stats = store.compress_trace_if_needed(run_id)
        assert stats is None
        assert (store.runs_dir / run_id / "trace.jsonl").is_file()

    def test_compress_above_threshold(self, store, monkeypatch):
        monkeypatch.setenv("FIXLOOP_TRACE_GZIP_LINES", "2")
        run_id = "gzip-test-003"
        for i in range(5):
            store.append_trace_event(run_id, "tool_executed", {"tool": f"t{i}"})
        stats = store.compress_trace_if_needed(run_id)
        assert stats is not None
        assert stats["compressed_bytes"] > 0
        assert stats["original_bytes"] > stats["compressed_bytes"]
        assert stats["compression_ratio"] < 1.0
        assert not (store.runs_dir / run_id / "trace.jsonl").is_file()
        assert (store.runs_dir / run_id / "trace.jsonl.gz").is_file()

    def test_read_trace_lines_from_gz(self, store, monkeypatch):
        monkeypatch.setenv("FIXLOOP_TRACE_GZIP_LINES", "2")
        run_id = "gzip-test-004"
        for i in range(5):
            store.append_trace_event(run_id, "tool_executed", {"tool": f"t{i}"})
        store.compress_trace_if_needed(run_id)
        lines = store.read_trace_lines(run_id)
        assert len(lines) == 5
        assert json.loads(lines[0])["event"] == "tool_executed"

    def test_threshold_zero_disables_compression(self, store, monkeypatch):
        monkeypatch.setenv("FIXLOOP_TRACE_GZIP_LINES", "0")
        run_id = "gzip-test-005"
        for i in range(50):
            store.append_trace_event(run_id, "tool_executed", {"tool": f"t{i}"})
        stats = store.compress_trace_if_needed(run_id)
        assert stats is None
        assert (store.runs_dir / run_id / "trace.jsonl").is_file()

    def test_invalid_env_var_falls_back_to_default(self):
        from agent_runtime.run_store import _trace_gzip_threshold

        assert _trace_gzip_threshold() == 1000

    def test_replay_runner_reads_gz_transparently(self, tmp_path):
        import gzip

        gz = tmp_path / "trace.jsonl.gz"
        events = [
            {"event": "run_started", "created_at": "2026-01-01T00:00:00Z"},
            {"event": "tool_executed", "payload": {"tool": "read_file"}},
        ]
        with gzip.open(gz, "wt", encoding="utf-8") as f:
            for e in events:
                f.write(json.dumps(e) + "\n")

        from agent_runtime.replay import ReplayRunner

        runner = ReplayRunner(str(tmp_path / "trace.jsonl"))
        result = runner.replay(None)
        assert result.total == 1
        assert len(result.errors) == 0
