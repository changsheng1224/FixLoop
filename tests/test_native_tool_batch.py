"""T1/T2/T4/T5/T6/T8/T9 through the production native AgentLoop path."""

import io
import json
import threading
from concurrent.futures import ThreadPoolExecutor

import pytest

from agent_runtime.agent_loop import AgentLoop
from agent_runtime.callbacks import CLIProgressCallback
from agent_runtime.cancellation import CancellationToken
from agent_runtime.config import AgentConfig
from agent_runtime.model_turn import FinishKind, ModelTurnResult, ProviderFinish, ToolCall
from agent_runtime.providers.clients import FakeNativeToolClient
from agent_runtime.runtime import Agent
from agent_runtime.tool_batch import ToolBatchProtocolError
from agent_runtime.tool_executor import QuotaEnforcer
from agent_runtime.tool_result import ToolResult
from agent_runtime.turn_progress import replay_progress


class BatchClient(FakeNativeToolClient):
    def __init__(self, calls):
        super().__init__(["<final>done</final>"])
        self.calls = calls
        self.requests = []

    def complete_turn(self, request):
        self.requests.append(request)
        if len(self.requests) == 1:
            return ModelTurnResult(
                tool_calls=self.calls, finish=ProviderFinish(FinishKind.TOOL_CALLS)
            )
        return super().complete_turn(request)


def make_agent(workspace, calls):
    client = BatchClient(calls)
    agent = Agent(
        AgentConfig(provider="fake", max_steps=5, approval="auto", loop_detect_threshold=0),
        client,
        workspace,
        cwd=workspace.cwd,
    )
    return agent, client


def test_native_same_tool_real_read_pairing_live_cli_and_trace(workspace, temp_workspace):
    agent, client = make_agent(
        workspace,
        [
            ToolCall("read_file", {"path": "README.md"}, "provider-a"),
            ToolCall("read_file", {"path": "pyproject.toml"}, "provider-b"),
        ],
    )
    barrier = threading.Barrier(2)
    original = agent.tools["read_file"]["run_with_context"]
    contexts = {}

    def slow_read(ctx, args):
        contexts[args["path"]] = ctx
        barrier.wait(timeout=3)
        return original(ctx, args)

    agent.tools["read_file"]["run_with_context"] = slow_read
    output = io.StringIO()
    cli = CLIProgressCallback(output)
    loop = AgentLoop(agent)
    assert "done" in loop.run("inspect both files", callback=cli, skip_plan=True)
    blocks = client.requests[1].messages[-1]["content"]
    assert [block["tool_use_id"] for block in blocks] == ["provider-a", "provider-b"]
    assert "Test Project" in blocks[0]["content"]
    assert "name='test'" in blocks[1]["content"]
    observations = agent.session["tool_observations"]
    assert [obs["call_id"] for obs in observations] == ["provider-a", "provider-b"]
    receipts = [action["receipt"] for action in agent.session["action_ledger"]]
    assert [receipt["call_id"] for receipt in receipts] == ["provider-a", "provider-b"]
    assert len({receipt["receipt_id"] for receipt in receipts}) == 2
    assert len({action["idempotency_key"] for action in agent.session["action_ledger"]}) == 2
    assert all(
        action["result_ref"] == obs["observation_id"]
        for action, obs in zip(agent.session["action_ledger"], observations)
    )
    assert contexts["README.md"] is not contexts["pyproject.toml"]
    assert contexts["README.md"].cancel_token is None  # restored independent context
    assert agent.tool_context.cancel_token is None
    assert "_last_canonical_tool_call" not in agent.session
    assert "running=2" in output.getvalue()
    trace_path = next((temp_workspace / ".agent" / "runs").glob("*/trace.jsonl"))
    trace = [json.loads(line) for line in trace_path.read_text(encoding="utf-8").splitlines()]
    events = [event["payload"] for event in trace if "event_seq" in event.get("payload", {})]
    # Replay each Turn with its own sequence domain.
    tool_turn = events[0]["turn_id"]
    same_turn = [event for event in events if event["turn_id"] == tool_turn]
    assert not replay_progress(same_turn)["progress_replay_incomplete"]
    assert replay_progress(events) == cli.progress.snapshot()
    assert "Test Project" not in json.dumps(events)
    cp = agent.session["checkpoints"][-1]["turn_progress"]
    assert cp["turn_id"]
    assert agent.quota.quota_summary()["total"]["used"] == 2


def test_native_permission_rejection_does_not_block_valid_call(workspace):
    agent, client = make_agent(
        workspace,
        [
            ToolCall("read_file", {"path": "../../outside.txt"}, "denied"),
            ToolCall("read_file", {"path": "README.md"}, "valid"),
        ],
    )
    loop = AgentLoop(agent)
    loop.run("inspect", skip_plan=True)
    assert agent.session["tool_observations"][0]["status"] == "validation_error"
    assert agent.session["tool_observations"][1]["status"] == "success"
    assert [b["tool_use_id"] for b in client.requests[1].messages[-1]["content"]] == [
        "denied",
        "valid",
    ]


def test_native_quota_reservations_prevent_overspend(workspace):
    agent, _ = make_agent(
        workspace,
        [ToolCall("read_file", {"path": "README.md", "start": i}, f"c{i}") for i in range(1, 5)],
    )
    agent.quota = QuotaEnforcer(max_total=1)
    AgentLoop(agent).run("inspect", skip_plan=True)
    assert agent.quota.quota_summary()["total"]["used"] == 1
    assert (
        sum(o["failure_class"] == "quota_exceeded" for o in agent.session["tool_observations"]) == 3
    )
    assert len(agent.session["tool_observations"]) == 4


def test_native_mixed_write_keeps_pairing_and_invalidates_old_read(workspace, temp_workspace):
    agent, client = make_agent(
        workspace,
        [
            ToolCall("read_file", {"path": "README.md"}, "before"),
            ToolCall("write_file", {"path": "README.md", "content": "changed\n"}, "write"),
            ToolCall("read_file", {"path": "README.md"}, "after"),
        ],
    )
    AgentLoop(agent).run("inspect then modify", skip_plan=True)
    blocks = client.requests[1].messages[-1]["content"]
    assert [b["tool_use_id"] for b in blocks] == ["before", "write", "after"]
    assert "Test Project" not in blocks[0]["content"]
    assert "tool_write_changed_dependency" in blocks[0]["content"]
    assert "freshness=stale" in blocks[0]["content"]
    assert "changed" in blocks[2]["content"]
    assert (temp_workspace / "README.md").read_text() == "changed\n"


@pytest.mark.parametrize(
    "calls",
    [
        [ToolCall("read_file", {"path": "README.md"}, "")],
        [ToolCall("read_file", {}, "same"), ToolCall("read_file", {}, "same")],
        [ToolCall("unregistered", {}, "x")],
    ],
)
def test_native_protocol_failure_executes_nothing(workspace, calls):
    agent, _ = make_agent(workspace, calls)
    with pytest.raises(ToolBatchProtocolError):
        AgentLoop(agent).run("inspect", skip_plan=True)
    assert not agent.session.get("tool_observations")
    assert agent.quota.quota_summary()["total"]["used"] == 0


def test_native_cancel_keeps_all_result_receipts(workspace):
    agent, _ = make_agent(
        workspace,
        [ToolCall("read_file", {"path": "README.md", "start": i}, f"c{i}") for i in range(1, 5)],
    )
    token = CancellationToken()
    agent.cancel_token = token
    barrier = threading.Barrier(2)
    started = threading.Event()
    executed = []

    def slow(ctx, args):
        executed.append(args["start"])
        barrier.wait(timeout=3)
        started.set()
        while not ctx.cancel_token.is_cancelled:
            threading.Event().wait(0.005)
        return ToolResult(
            content="stopped", status="cancelled", metadata={"termination_guaranteed": True}
        )

    agent.tools["read_file"]["run_with_context"] = slow
    with ThreadPoolExecutor(max_workers=1) as owner:
        future = owner.submit(AgentLoop(agent).run, "inspect", skip_plan=True)
        # Wait for tool execution, rather than timing cold runtime/model initialization.
        assert started.wait(timeout=30)
        token.cancel()
        future.result(timeout=5)
    assert sorted(executed) == [1, 2]
    assert [o["call_id"] for o in agent.session["tool_observations"]] == ["c1", "c2", "c3", "c4"]
    assert all(o["status"] == "cancelled" for o in agent.session["tool_observations"])
    assert len(agent.session["turn_progress"]["calls"]) == 4


def test_native_file_version_change_is_partial_and_does_not_grant_edit_lock(
    workspace, temp_workspace
):
    from types import SimpleNamespace

    agent, _ = make_agent(
        workspace,
        [
            ToolCall("read_file", {"path": "README.md"}, "old"),
            ToolCall("read_file", {"path": "pyproject.toml"}, "stable"),
        ],
    )
    read_complete = threading.Event()
    changed = threading.Event()
    marked = []
    agent.tool_context.edit_lock = SimpleNamespace(mark_read=lambda path, **_: marked.append(path))
    original = agent.tools["read_file"]["run_with_context"]

    def slow(ctx, args):
        result = original(ctx, args)
        if args["path"] == "README.md":
            read_complete.set()
            changed.wait(timeout=3)
        return result

    agent.tools["read_file"]["run_with_context"] = slow
    with ThreadPoolExecutor(max_workers=1) as owner:
        future = owner.submit(AgentLoop(agent).run, "inspect", skip_plan=True)
        assert read_complete.wait(timeout=3)
        (temp_workspace / "README.md").write_text("external change\n")
        changed.set()
        assert "done" in future.result(timeout=5)
    assert agent.session["tool_observations"][0]["status"] == "partial"
    assert agent.session["action_ledger"][0]["status"] == "failed"
    assert "README.md" not in marked and "pyproject.toml" in marked


def test_native_run_budget_rejections_still_have_ordered_observations(workspace):
    agent, client = make_agent(
        workspace,
        [ToolCall("read_file", {"path": "README.md", "start": i}, f"c{i}") for i in range(1, 5)],
    )
    loop = AgentLoop(agent)
    loop._repair_budget.max_tool_calls = 1
    loop.run("inspect", skip_plan=True)
    observations = agent.session["tool_observations"]
    assert [obs["call_id"] for obs in observations] == ["c1", "c2", "c3", "c4"]
    assert sum(obs["failure_class"] == "budget_exceeded" for obs in observations) == 3
    assert [b["tool_use_id"] for b in client.requests[1].messages[-1]["content"]] == [
        "c1",
        "c2",
        "c3",
        "c4",
    ]


def test_native_group_budget_reservation_is_atomic(workspace):
    agent, _ = make_agent(
        workspace,
        [ToolCall("read_file", {"path": "README.md", "start": i}, f"c{i}") for i in range(1, 5)],
    )
    agent.quota = QuotaEnforcer(max_total=10, group_limits={"read": 1})
    AgentLoop(agent).run("inspect", skip_plan=True)
    summary = agent.quota.quota_summary()
    assert summary["groups"]["read"]["used"] == 1
    assert summary["total"]["used"] == 1
    assert (
        sum(o["failure_class"] == "quota_exceeded" for o in agent.session["tool_observations"]) == 3
    )


def test_native_serial_side_effect_retains_inflight_action_and_sequential_steps(workspace):
    from agent_runtime.callbacks import AgentCallback

    agent, _ = make_agent(
        workspace,
        [
            ToolCall("read_file", {"path": "README.md"}, "before"),
            ToolCall("write_file", {"path": "README.md", "content": "new"}, "write"),
            ToolCall("read_file", {"path": "README.md"}, "after"),
        ],
    )
    loop = AgentLoop(agent)
    original = agent.tools["write_file"]["run"]

    def writing(args):
        assert loop._tool_state.in_flight_tool == "write_file"
        action = agent.session["_in_flight_action"]
        assert action["status"] == "dispatched"
        assert ":write:" in action["idempotency_key"]
        return original(args)

    agent.tools["write_file"]["run"] = writing
    steps = []

    class Tracker(AgentCallback):
        def on_pre_tool(self, step, tool_name, tool_args, **_):
            steps.append(step)

    assert "done" in loop.run("inspect", callback=Tracker(), skip_plan=True)
    assert steps == [1, 2, 3]
    assert "_in_flight_action" not in agent.session


def test_native_partial_read_is_not_verified_or_silently_reused(workspace, temp_workspace):
    from agent_runtime.code_exploration.models import RetrievalLimits

    agent, client = make_agent(
        workspace,
        [
            ToolCall("read_file", {"path": "README.md"}, "partial"),
            ToolCall("read_file", {"path": "pyproject.toml"}, "complete"),
        ],
    )
    (temp_workspace / "README.md").write_text("x" * 100)
    agent.tool_context.exploration_limits = RetrievalLimits(range_scan_bytes=40)
    AgentLoop(agent).run("inspect", skip_plan=True)
    assert agent.session["tool_observations"][0]["status"] == "partial"
    assert agent.session["action_ledger"][0]["status"] == "failed"
    assert "partial_result" in client.requests[1].messages[-1]["content"][0]["content"]
    assert agent.session["tool_observations"][1]["status"] == "success"


def test_native_oversize_batch_returns_every_result_in_order(workspace):
    agent, client = make_agent(
        workspace,
        [ToolCall("read_file", {"path": "README.md", "end": i}, f"c{i}") for i in range(1, 6)],
    )
    AgentLoop(agent).run("inspect", skip_plan=True)
    assert [b["tool_use_id"] for b in client.requests[1].messages[-1]["content"]] == [
        "c1",
        "c2",
        "c3",
        "c4",
        "c5",
    ]
    assert len(agent.session["tool_observations"]) == 5
