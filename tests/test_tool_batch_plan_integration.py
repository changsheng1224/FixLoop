"""Plan and nested native tools share two physical read permits and receipts."""

import threading

from agent_runtime.agent_loop import AgentLoop
from agent_runtime.cancellation import CancellationToken
from agent_runtime.model_turn import ToolCall
from agent_runtime.plan_runtime.scheduler import PlanScheduler
from agent_runtime.read_permits import ReadPermitPool
from agent_runtime.workspace import WorkspaceContext
from tests.plan_support import session_for, simple_plan
from tests.test_native_tool_batch import make_agent
from tests.test_tool_batch import batch_for, scheduler_for


def test_two_plan_nodes_each_native_batch_never_create_four_readers(tmp_path):
    (tmp_path / "value.py").write_text("value = 1\nother = 2\n")
    workspace = WorkspaceContext.build(str(tmp_path))
    barrier = threading.Barrier(2)
    lock = threading.Lock()
    active = peak = 0
    with session_for(tmp_path) as session:
        session.create(simple_plan(session, reads=2))
        session.configure_long_task("inspect both ranges")
        agents = [
            make_agent(
                workspace,
                [
                    ToolCall("read_file", {"path": "value.py", "start": j}, f"p{i}-{j}")
                    for j in (1, 2)
                ],
            )[0]
            for i in range(2)
        ]
        for agent in agents:
            agent._plan_session = session
            agent.shared_run_id = session.identity["run_id"]
            reader = agent.tools["read_file"]["run_with_context"]

            def slow(ctx, args, reader=reader):
                nonlocal active, peak
                with lock:
                    active += 1
                    peak = max(peak, active)
                try:
                    barrier.wait(timeout=5)
                    return reader(ctx, args)
                finally:
                    with lock:
                        active -= 1

            agent.tools["read_file"]["run_with_context"] = slow

        def callback(agent):
            def invoke(attempt):
                assert "done" in AgentLoop(agent).run("inspect", skip_plan=True)
                return session.tool_result(attempt)

            return invoke

        PlanScheduler(session).run_reads(
            {f"read-{i}": callback(agent) for i, agent in enumerate(agents)}
        )
        assert peak == 2
        operations = session.store.latest("operation", "operation_id").values()
        assert {op["call_id"] for op in operations} == {"p0-1", "p0-2", "p1-1", "p1-2"}
        assert all(
            op["receipt"]["call_id"] == op["call_id"] and op["batch_id"] and op["turn_id"]
            for op in operations
        )
        assert all(node.status == "succeeded" for node in session.plan.nodes[:2])


def test_borrowed_permit_remains_occupied_until_abandoned_child_exits(tmp_path):
    pool = ReadPermitPool()
    started = threading.Event()
    release = threading.Event()
    token = CancellationToken()
    batch = batch_for(tmp_path, [ToolCall("read_file", {}, "a")])

    def prepare(call):
        def work():
            started.set()
            token.cancel()
            release.wait(timeout=3)
            return "late"

        return work

    scheduler, _ = scheduler_for(pool, cleanup_s=0.01)
    try:
        with pool.lease():
            results = scheduler.run(batch, prepare, lambda call, result: result, cancel_token=token)
        assert started.is_set() and results[0].status == "uncertain"
        assert pool.acquire()
        assert not pool.acquire()  # parent has exited; orphan still occupies one
        pool.release()
    finally:
        release.set()
