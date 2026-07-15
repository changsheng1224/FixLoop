"""Deterministic Replay：从 trace.jsonl 回放工具执行 + 树状摘要。

不重新调模型（结果不确定），只读 trace 文件做审计/调试。
"""

import json
from dataclasses import dataclass, field
from pathlib import Path

from agent_runtime.run_store import read_trace_path


def trace_tree_summary(run_dir: str | Path) -> str:
    """从 trace.jsonl 生成树状摘要（只读，不重调模型）。

    输出包括：run_started → context_built → tool_executed → run_finished。
    每步工具调用显示 tool name + preview + timing。
    """
    path = Path(run_dir) / "trace.jsonl"
    raw_lines = read_trace_path(path)
    trace_exists = path.is_file() or path.with_suffix(".jsonl.gz").is_file()
    if not raw_lines:
        return "(empty trace)" if trace_exists else f"(trace not found: {path})"

    lines = []
    events = []
    for line_text in raw_lines:
        if not line_text.strip():
            continue
        try:
            events.append(json.loads(line_text))
        except json.JSONDecodeError:
            pass

    if not events:
        return "(empty trace)"

    lines.append(f"Trace: {len(events)} events")
    lines.append("─" * 40)

    step = 0
    for ev in events:
        name = ev.get("event", "?")
        payload = ev.get("payload", {})

        if name == "run_started":
            lines.append("▶ run_started")
        elif name == "context_built":
            sections = payload.get("sections") or payload.get("context_sections") or {}
            total = payload.get("total_tokens", "?")
            lines.append(f"  📋 context_built ({total} tokens)")
            for sname, stokens in sorted(sections.items()):
                lines.append(f"      {sname}: {stokens}")
        elif name == "tool_executed":
            step += 1
            tool = payload.get("tool", "?")
            tier = payload.get("execution_tier", "")
            lines.append(f"  [{step}] 🔧 {tool}" + (f" ({tier})" if tier else ""))
        elif name == "model_request_start":
            lines.append("  🤖 model_request")
        elif name == "run_finished":
            reason = payload.get("stop_reason", "?")
            lines.append(f"■ run_finished ({reason})")

    return "\n".join(lines)


def trace_step_prompt(run_dir: str | Path, step: int) -> str:
    """从 trace 中提取第 N 步的 prompt 文本（来自 context_built）。"""
    path = Path(run_dir) / "trace.jsonl"
    raw_lines = read_trace_path(path)
    if not raw_lines:
        return "(trace not found)"

    context_seen = 0
    for line_text in raw_lines:
        if not line_text.strip():
            continue
        try:
            ev = json.loads(line_text)
        except json.JSONDecodeError:
            continue
        if ev.get("event") == "context_built":
            context_seen += 1
            if context_seen == step:
                payload = ev.get("payload", {})
                preview = payload.get("prompt_preview", "")
                if preview:
                    return preview[:2000]
                return json.dumps(payload, indent=2, ensure_ascii=False)[:2000]
    return f"(step {step} not found in trace)"


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
