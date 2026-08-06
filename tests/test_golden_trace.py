"""Golden trace 回归单测：关键事件名序列 golden diff。"""

import json
from pathlib import Path

# Golden event sequence（精简后的关键事件名）
GOLDEN_EVENT_NAMES = [
    "repair_started",
    "agent_ask_started",  # localizer
    "agent_ask_finished",
    "agent_ask_started",  # retriever
    "agent_ask_finished",
    "skill_matched",  # skill resolution
    "blackboard_written",  # blackboard merge
    "agent_ask_started",  # patcher
    "agent_ask_finished",
]


def _event_name_sequence(trace_path: Path) -> list[str]:
    """提取 trace.jsonl 中 REPAIR_TRACE_EVENTS 的事件名序列。"""
    from src.repair.run_trace import REPAIR_TRACE_EVENTS

    names = []
    for line in trace_path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            ev = json.loads(line)
        except json.JSONDecodeError:
            continue
        name = ev.get("event", "")
        if name in REPAIR_TRACE_EVENTS:
            names.append(name)
    return names


def _event_names_match(actual: list[str], golden: list[str]) -> tuple[bool, list[str]]:
    """检查 actual 是否为 golden 的超序列（子序列匹配）。"""
    gi = 0
    for name in actual:
        if gi < len(golden) and name == golden[gi]:
            gi += 1
    if gi == len(golden):
        return True, []
    missing = golden[gi:]
    return False, missing


class TestGoldenTrace:
    def test_golden_event_names_cover_key_phases(self):
        """Golden 事件名覆盖核心修复阶段。"""
        assert "repair_started" in GOLDEN_EVENT_NAMES
        assert "agent_ask_started" in GOLDEN_EVENT_NAMES
        assert "agent_ask_finished" in GOLDEN_EVENT_NAMES

    def test_golden_sequence_is_subsequence_of_fake_run(self, tmp_path):
        """Fake eval 产生的 trace 应包含 golden 事件序列。"""
        from src.repair.run_trace import RepairRunTracer

        tracer = RepairRunTracer(str(tmp_path))
        rid = tracer.begin("test issue")
        tracer.emit("orchestrator", "agent_ask_started", {"agent": "patcher"})
        tracer.emit("orchestrator", "agent_ask_finished", {"agent": "patcher"})
        tracer.emit("orchestrator", "agent_ask_started", {"agent": "verifier"})
        tracer.emit("orchestrator", "agent_ask_finished", {"agent": "verifier"})
        tracer.emit("orchestrator", "skill_matched", {})
        tracer.emit("orchestrator", "blackboard_written", {})
        tracer.emit("orchestrator", "agent_ask_started", {"agent": "patcher"})
        tracer.emit("orchestrator", "agent_ask_finished", {"agent": "patcher"})

        trace_path = tracer.store.runs_dir / rid / "trace.jsonl"
        actual = _event_name_sequence(trace_path)
        match, missing = _event_names_match(actual, GOLDEN_EVENT_NAMES)
        assert match, f"missing golden events: {missing}"

    def test_all_golden_events_are_valid_trace_events(self):
        """Golden 事件名都在 REPAIR_TRACE_EVENTS 集合中。"""
        from src.repair.run_trace import REPAIR_TRACE_EVENTS

        for name in GOLDEN_EVENT_NAMES:
            assert name in REPAIR_TRACE_EVENTS, f"'{name}' not in REPAIR_TRACE_EVENTS"

    def test_golden_sequence_order_is_correct(self):
        """Golden 事件序列符合修复流程的时间顺序。"""
        assert GOLDEN_EVENT_NAMES.index("repair_started") == 0
        # localize before retrieve before patch (3 agent_ask_started)
        indices = [i for i, n in enumerate(GOLDEN_EVENT_NAMES) if n == "agent_ask_started"]
        assert len(indices) == 3
        assert indices[0] < indices[1] < indices[2]
