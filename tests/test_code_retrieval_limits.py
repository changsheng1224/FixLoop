"""Actual read and scope limits, including sensitive descendants."""

import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from agent_runtime.code_exploration.io import grep_result, list_files_result, read_file_result
from agent_runtime.code_exploration.models import RetrievalLimits
from agent_runtime.tool_context import ToolContext


def _context(root, **limits):
    context = ToolContext(root=str(root))
    context.exploration_limits = RetrievalLimits(**limits)
    return context


def test_deep_line_is_partial_at_actual_byte_limit(tmp_path):
    (tmp_path / "deep.py").write_bytes(b"x\n" * 10000)
    result = read_file_result(
        _context(tmp_path, range_scan_bytes=128),
        {"path": "deep.py", "start": 5000, "end": 5001},
    )
    retrieval = result.metadata["retrieval_result"]
    assert retrieval["budget_used"]["bytes_read"] == 128
    assert retrieval["completeness"] == "partial"
    assert "scan_bytes" in retrieval["truncation_reasons"]
    assert "超出文件行数" not in result.content


def test_reported_read_bytes_match_stream_reads(tmp_path, monkeypatch):
    target = tmp_path / "counted.py"
    target.write_bytes(b"x\n" * 1000)
    real_open = Path.open
    measured = 0

    class MeteredStream:
        def __init__(self, stream):
            self.stream = stream

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return self.stream.__exit__(*args)

        def readline(self, limit):
            nonlocal measured
            data = self.stream.readline(limit)
            measured += len(data)
            return data

        def tell(self):
            return self.stream.tell()

    def metered_open(path, *args, **kwargs):
        stream = real_open(path, *args, **kwargs)
        return MeteredStream(stream) if path == target and args[0] == "rb" else stream

    monkeypatch.setattr(Path, "open", metered_open)
    result = read_file_result(
        _context(tmp_path, range_scan_bytes=128),
        {"path": "counted.py", "start": 500, "end": 501},
    )
    assert measured == result.metadata["retrieval_result"]["budget_used"]["bytes_read"] == 128


def test_long_line_and_binary_are_bounded(tmp_path):
    (tmp_path / "long.py").write_bytes(b"a" * 10000 + b"\n")
    result = read_file_result(
        _context(tmp_path, range_scan_bytes=2048, line_bytes=80), {"path": "long.py"}
    )
    retrieval = result.metadata["retrieval_result"]
    assert retrieval["budget_used"]["bytes_read"] <= 2048
    assert "scan_bytes" in retrieval["truncation_reasons"]
    assert "long_line" in retrieval["truncation_reasons"]
    (tmp_path / "binary.py").write_bytes(b"\x00private\n")
    binary = read_file_result(_context(tmp_path), {"path": "binary.py"})
    assert "二进制" in binary.content
    assert "private" not in binary.content


def test_late_binary_marker_discards_prior_search_hits(tmp_path, monkeypatch):
    from agent_runtime.code_exploration import io

    (tmp_path / "large.py").write_bytes(b"needle\n" + b"x" * 4096 + b"\x00SECRET\n")
    monkeypatch.setattr(io.shutil, "which", lambda command: None)
    result = grep_result(_context(tmp_path), {"pattern": "needle"})
    retrieval = result.metadata["retrieval_result"]
    assert retrieval["hits"] == []
    assert "needle" not in result.content
    assert "SECRET" not in result.content


def test_search_file_and_read_limits_are_observable(tmp_path):
    for index in range(5):
        (tmp_path / f"file{index}.py").write_bytes(b"x" * 80 + b"\nneedle\n")
    result = grep_result(
        _context(tmp_path, search_files=2, search_read_bytes=100),
        {"pattern": "needle", "path": "."},
    )
    retrieval = result.metadata["retrieval_result"]
    assert retrieval["budget_used"]["bytes_read"] <= 100
    assert retrieval["budget_used"]["files_scanned"] <= 2
    assert retrieval["completeness"] == "partial"
    assert retrieval["truncation_reasons"]


def test_rg_unavailable_uses_bounded_python_fallback(tmp_path, monkeypatch):
    from agent_runtime.code_exploration import io

    (tmp_path / "a.py").write_text("needle\n", encoding="utf-8")
    monkeypatch.setattr(io.shutil, "which", lambda command: None)
    result = grep_result(_context(tmp_path), {"pattern": "needle"})
    retrieval = result.metadata["retrieval_result"]
    assert retrieval["degradation_reason"] == "rg_unavailable"
    assert retrieval["hits"][0]["path"] == "a.py"
    assert retrieval["budget_used"]["bytes_read"] == (tmp_path / "a.py").stat().st_size


def test_expired_deadline_is_partial_not_empty_complete(tmp_path):
    (tmp_path / "a.py").write_text("needle\n", encoding="utf-8")
    context = _context(tmp_path)
    context.deadline = SimpleNamespace(remaining_s=lambda: 0)
    result = grep_result(context, {"pattern": "needle"})
    retrieval = result.metadata["retrieval_result"]
    assert retrieval["execution"] == "timeout"
    assert retrieval["completeness"] == "partial"
    assert result.status == "error"


def test_sensitive_child_and_escape_are_not_exposed(tmp_path):
    (tmp_path / "safe.py").write_text("marker\n", encoding="utf-8")
    (tmp_path / "secrets.json").write_text("marker PRIVATE\n", encoding="utf-8")
    context = _context(tmp_path)
    result = grep_result(context, {"pattern": "marker"})
    assert "safe.py" in result.content
    assert "PRIVATE" not in result.content
    listing = list_files_result(context, {"path": "."})
    assert "secrets.json" not in listing.content
    escaped = read_file_result(context, {"path": "../outside"})
    assert "路径逃逸" in escaped.content
    assert escaped.error_code == "path_outside_workspace"


def test_symlink_escape_is_skipped(tmp_path):
    outside = tmp_path.parent / "outside-code-retrieval.py"
    outside.write_text("SECRET_ESCAPE\n", encoding="utf-8")
    try:
        os.symlink(outside, tmp_path / "linked.py")
    except (OSError, NotImplementedError):
        pytest.skip("symlink creation unavailable")
    result = grep_result(_context(tmp_path), {"pattern": "SECRET_ESCAPE"})
    assert "SECRET_ESCAPE" not in result.content
