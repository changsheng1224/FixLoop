"""CC 序 compact + thrash。"""

from __future__ import annotations

from src.repair.context_compact import CompactResult, compact_tool_history


def test_compact_drops_old_successful_reads():
    history = [
        {"role": "user", "content": "seed"},
        {"role": "assistant", "content": "调用工具: read_file", "tool_name": "read_file"},
        {"role": "user", "content": "# a.py\n" + ("line\n" * 200)},
        {"role": "assistant", "content": "调用工具: read_file", "tool_name": "read_file"},
        {"role": "user", "content": "# b.py\n" + ("line\n" * 200)},
        {"role": "assistant", "content": "调用工具: grep", "tool_name": "grep"},
        {"role": "user", "content": "Error: fail near=foo"},
    ]
    result = compact_tool_history(history, keep_failed=2, max_success_chars=80)
    assert isinstance(result, CompactResult)
    assert result.compacted is True
    # 失败保留
    assert any("Error" in (m.get("content") or "") for m in result.messages)
    # 大成功输出被截短
    big = [m for m in result.messages if "a.py" in (m.get("content") or "")]
    if big:
        assert len(big[0]["content"]) <= 200


def test_compact_thrash_stops():
    history = [
        {"role": "user", "content": "x" * 5000},
        {"role": "assistant", "content": "ok"},
    ]
    r1 = compact_tool_history(history, thrash_threshold=2, prior_compacts=1)
    assert r1.compacted
    r2 = compact_tool_history(
        r1.messages, thrash_threshold=2, prior_compacts=r1.compact_count
    )
    # 再次仍爆 → thrash
    bloated = r2.messages + [{"role": "user", "content": "y" * 5000}]
    r3 = compact_tool_history(
        bloated, thrash_threshold=2, prior_compacts=2, max_total_chars=1000
    )
    assert r3.thrash is True
    assert r3.compact_thrash_count >= 1
