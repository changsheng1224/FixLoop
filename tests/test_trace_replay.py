"""Trace replay 单测：树状摘要 + prompt 提取。"""

import json
import tempfile
from pathlib import Path


def _write_trace(run_dir: Path):
    events = [
        {"event": "run_started", "payload": {}},
        {"event": "context_built", "payload": {
            "sections": {"system": 200, "tools": 150, "request": 100},
            "total_tokens": 450,
        }},
        {"event": "tool_executed", "payload": {"tool": "read_file", "execution_tier": "host"}},
        {"event": "tool_executed", "payload": {"tool": "grep", "execution_tier": "host"}},
        {"event": "run_finished", "payload": {"stop_reason": "final"}},
    ]
    with open(run_dir / "trace.jsonl", "w", encoding="utf-8") as f:
        for ev in events:
            f.write(json.dumps(ev) + "\n")


class TestTraceTreeSummary:
    def test_empty_trace_file(self, tmp_path):
        from agent_runtime.replay import trace_tree_summary

        run_dir = tmp_path / "empty"
        run_dir.mkdir()
        (run_dir / "trace.jsonl").write_text("")
        result = trace_tree_summary(run_dir)
        assert "empty" in result.lower()

    def test_summary_includes_events(self, tmp_path):
        from agent_runtime.replay import trace_tree_summary

        run_dir = tmp_path / "r1"
        run_dir.mkdir()
        _write_trace(run_dir)
        result = trace_tree_summary(run_dir)
        assert "run_started" in result
        assert "context_built" in result
        assert "read_file" in result
        assert "grep" in result
        assert "run_finished" in result

    def test_missing_trace_file(self, tmp_path):
        from agent_runtime.replay import trace_tree_summary

        result = trace_tree_summary(str(tmp_path / "nonexistent"))
        assert "not found" in result


class TestTraceStepPrompt:
    def test_step_prompt_extraction(self, tmp_path):
        from agent_runtime.replay import trace_step_prompt

        run_dir = tmp_path / "r2"
        run_dir.mkdir()
        _write_trace(run_dir)
        result = trace_step_prompt(run_dir, step=1)
        # 第1个 context_built 含 sections
        assert "200" in result or "system" in result

    def test_step_not_found(self, tmp_path):
        from agent_runtime.replay import trace_step_prompt

        result = trace_step_prompt(str(tmp_path / "nonexistent"), step=99)
        assert "not found" in result
