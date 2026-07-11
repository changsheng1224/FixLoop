"""Deterministic Replay：从 trace.jsonl（或 .gz）回放工具执行，对比结果差异。

不重新调模型（结果不确定），只回放工具执行（结果确定）。
"""

import json
from dataclasses import dataclass, field
from pathlib import Path

from agent_runtime.run_store import read_trace_path


@dataclass
class ReplayResult:
    """回放结果。"""

    matches: int = 0
    diffs: list[dict] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    total: int = 0

    @property
    def all_match(self) -> bool:
        """回放结果与 trace 是否完全一致（无 diff 且无 error）。"""
        return len(self.diffs) == 0 and len(self.errors) == 0


class ReplayRunner:
    """从 trace 文件回放工具执行并对比结果。"""

    def __init__(self, trace_path: str):
        self.trace_path = Path(trace_path)

    def replay(self, agent) -> ReplayResult:
        """回放 trace 中的所有 tool_executed 事件。

        Args:
            agent: Agent 实例（用于 execute_tool）。

        Returns:
            ReplayResult 包含匹配、差异和错误统计。
        """
        result = ReplayResult()

        lines = read_trace_path(self.trace_path)
        if not lines:
            result.errors.append(f"Trace file not found: {self.trace_path}")
            return result

        for line in lines:
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                result.errors.append(f"Invalid JSON: {line[:100]}")
                continue

            if event.get("event") != "tool_executed":
                continue

            payload = event.get("payload", {})
            tool_name = payload.get("tool", "")
            result.total += 1

            # 重新执行工具（需要参数信息）
            # trace 中只记录了 tool name，没有 args
            # 实际使用时需要从 history 重建 args
            result.diffs.append(
                {
                    "tool": tool_name,
                    "expected": "(trace recorded)",
                    "actual": "(args unavailable from trace alone)",
                }
            )

        return result
