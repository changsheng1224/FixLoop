"""Offline native AgentLoop batch demo; retain traces, receipts and timings.

Run from repository root: python scripts/demo_tool_batch_progress.py
The provider fixture emits one four-call response and then consumes its results.
Real audited filesystem readers run with a controlled 50ms delay for comparison.
"""

from __future__ import annotations

import argparse
import io
import json
import sys
import threading
import time
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from agent_runtime.agent_loop import AgentLoop
from agent_runtime.callbacks import CLIProgressCallback
from agent_runtime.config import AgentConfig
from agent_runtime.model_turn import FinishKind, ModelTurnResult, ProviderFinish, ToolCall
from agent_runtime.providers.clients import FakeNativeToolClient
from agent_runtime.runtime import Agent
from agent_runtime.turn_progress import replay_progress
from agent_runtime.workspace import WorkspaceContext


class NativeFixture(FakeNativeToolClient):
    def __init__(self):
        super().__init__(["<final>inspected</final>"])
        self.requests = []

    def complete_turn(self, request):
        self.requests.append(request)
        if len(self.requests) == 1:
            return ModelTurnResult(
                tool_calls=[
                    ToolCall("read_file", {"path": f"file-{i}.py"}, f"provider-{i}")
                    for i in range(4)
                ],
                finish=ProviderFinish(FinishKind.TOOL_CALLS),
            )
        return super().complete_turn(request)


def measure(directory: Path, parallel: bool):
    directory.mkdir(parents=True)
    for i in range(4):
        (directory / f"file-{i}.py").write_text(f"value = {i}\n", encoding="utf-8")
    provider = NativeFixture()
    agent = Agent(
        AgentConfig(provider="fake", max_steps=6, approval="auto", loop_detect_threshold=0),
        provider,
        WorkspaceContext.build(str(directory)),
        cwd=str(directory),
    )
    lock = threading.Lock()
    active = peak = 0
    spans = []
    reader = agent.tools["read_file"]["run_with_context"]

    def controlled(ctx, args):
        nonlocal active, peak
        started = time.perf_counter()
        with lock:
            active += 1
            peak = max(peak, active)
        try:
            threading.Event().wait(0.05)
            return reader(ctx, args)
        finally:
            ended = time.perf_counter()
            with lock:
                active -= 1
                spans.append({"path": args["path"], "started": started, "ended": ended})

    if parallel:
        agent.tools["read_file"]["run_with_context"] = controlled
    else:
        agent.tools["read_file"]["run"] = lambda args: controlled(agent.tool_context, args)
    output = io.StringIO()
    callback = CLIProgressCallback(output)
    started = time.perf_counter()
    loop = AgentLoop(agent)
    answer = loop.run("inspect four independent files", callback=callback, skip_plan=True)
    elapsed = time.perf_counter() - started
    traces = list((directory / ".agent" / "runs").glob("*/trace.jsonl"))
    trace = [json.loads(line) for line in traces[0].read_text(encoding="utf-8").splitlines()]
    progress = [event["payload"] for event in trace if "event_seq" in event.get("payload", {})]
    results = provider.requests[1].messages[-1]["content"]
    report = {
        "answer": answer,
        "parallel": parallel,
        "elapsed_s": round(elapsed, 4),
        "tool_window_s": round(
            max(s["ended"] for s in spans) - min(s["started"] for s in spans), 4
        ),
        "peak": peak,
        "call_ids": [r["tool_use_id"] for r in results],
        "results": [r["content"] for r in results],
        "quota": agent.quota.quota_summary(),
        "budget": loop._repair_budget.summary(),
        "observations": agent.session["tool_observations"],
        "receipts": [a["receipt"] for a in agent.session["action_ledger"]],
        "spans": spans,
        "progress_replay_matches": replay_progress(progress) == callback.progress.snapshot(),
        "trace": str(traces[0].resolve()),
    }
    (directory / "cli-progress.txt").write_text(output.getvalue(), encoding="utf-8")
    (directory / "report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return report


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", default="artifacts/tool-batch-progress")
    args = parser.parse_args()
    directory = Path(args.output).resolve() / uuid.uuid4().hex[:12]
    serial = measure(directory / "serial", False)
    parallel = measure(directory / "parallel", True)
    summary = {
        "provider": "offline native fixture",
        "reader_delay_ms": 50,
        "serial_elapsed_s": serial["elapsed_s"],
        "parallel_elapsed_s": parallel["elapsed_s"],
        "serial_tool_window_s": serial["tool_window_s"],
        "parallel_tool_window_s": parallel["tool_window_s"],
        "serial_peak": serial["peak"],
        "parallel_peak": parallel["peak"],
        "results_match": serial["results"] == parallel["results"],
        "budgets_match": serial["budget"] == parallel["budget"]
        and serial["quota"] == parallel["quota"],
        "replay_matches": serial["progress_replay_matches"] and parallel["progress_replay_matches"],
        "artifact_directory": str(directory),
    }
    assert summary["results_match"] and summary["budgets_match"] and summary["replay_matches"]
    assert summary["serial_peak"] == 1 and summary["parallel_peak"] == 2
    (directory / "summary.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
