"""D2: exercise real LangGraph scheduling against unchanged FixLoop ownership."""

import asyncio
import threading
from concurrent.futures import ThreadPoolExecutor
from contextvars import ContextVar
from dataclasses import dataclass
from typing import TypedDict

import pytest

pytest.importorskip("langgraph", reason="install .[dev,orchestration] for the D2 probe")

from langgraph.graph import END, START, StateGraph
from langgraph.runtime import Runtime

from agent_runtime.cancellation import CancellationToken, CancelledError
from tests.langgraph_execution_support import OwnerCalls
from tests.plan_l2_support import repair_fixture
from tests.plan_support import session_for, simple_plan


class Route(TypedDict):
    next_action: str


@dataclass
class ProbeContext:
    owner: OwnerCalls
    session: object
    state: object


@pytest.mark.parametrize("streaming", [False, True], ids=["invoke", "stream-updates"])
def test_real_patch_and_verification_remain_on_owner(tmp_path, streaming):
    session, state, _ = repair_fixture(tmp_path)
    owner = OwnerCalls()
    marker = ContextVar("d2_owner_marker", default="missing")
    binding = marker.set("owner-context")
    seen = []
    updates = []

    def patch(route, runtime: Runtime[ProbeContext]):
        ctx = runtime.context

        def execute():
            assert marker.get() == "owner-context"
            seen.append(("patch", threading.get_ident()))
            patches, meta = ctx.session.patcher_execution.run_patcher_toolized(
                ctx.state, "Read value.py and fix answer()", {}
            )
            assert patches, (meta, ctx.state.agent_errors)
            assert ctx.session.plan_binding.session.owner_thread == owner.owner_thread
            marker.set("after-patch")
            return {"next_action": "verify"}

        return ctx.owner.call(execute)

    def verify(route, runtime: Runtime[ProbeContext]):
        ctx = runtime.context

        def execute():
            assert marker.get() == "after-patch"
            seen.append(("verify", threading.get_ident()))
            result = ctx.session.verification.run_verifier(ctx.state)
            assert result.all_passed, result.failure_logs
            assert result.total_tests == 1
            ctx.state.verification_result = result
            return {"next_action": "done"}

        return ctx.owner.call(execute)

    builder = StateGraph(Route, context_schema=ProbeContext)
    builder.add_node("patch", patch)
    builder.add_node("verify", verify)
    builder.add_edge(START, "patch")
    builder.add_conditional_edges("patch", lambda route: route["next_action"], {"verify": "verify"})
    builder.add_edge("verify", END)
    graph = builder.compile()
    context = ProbeContext(owner, session, state)

    def execute_graph():
        if not streaming:
            return graph.invoke({"next_action": "patch"}, context=context)
        for update in graph.stream(
            {"next_action": "patch"}, context=context, stream_mode="updates"
        ):
            owner.call(lambda item=update: updates.append(item))

    try:
        owner.run(execute_graph)
        assert seen == [("patch", owner.owner_thread), ("verify", owner.owner_thread)]
        assert (tmp_path / "value.py").read_text() == "def answer():\n    return 2\n"
        assert state.verification_result.all_passed
        assert session.plan_binding.session.plan.status == "completed"
        assert state.control.plan_checkpoint
        plan = session.plan_binding.session
        edit_operations = plan.operations(plan.plan.node("edit").attempt_id)
        assert sum(op["tool"] == "patch_file" for op in edit_operations) == 1
        if streaming:
            assert [list(update) for update in updates] == [["patch"], ["verify"]]
    finally:
        if session.plan_binding:
            session.plan_binding.close()
        marker.reset(binding)


def test_graph_worker_can_enter_plan_only_through_owner(tmp_path):
    (tmp_path / "value.py").write_text("value = 1\n")
    with session_for(tmp_path) as plan:
        plan.create(simple_plan(plan, reads=0))
        owner = OwnerCalls()

        def stage(route):
            # Force a real worker boundary instead of relying on a serial fast path.
            with ThreadPoolExecutor(max_workers=1) as executor:
                with pytest.raises(ValueError, match="owner_thread_required"):
                    executor.submit(plan.prepare, "analysis").result()
                owner_id = executor.submit(owner.call, threading.get_ident).result()
                owner.call(lambda: plan.register_user_message("d2-message", "Keep the API stable"))
            return {"next_action": str(owner_id)}

        graph = StateGraph(Route).add_node("stage", stage)
        graph.add_edge(START, "stage").add_edge("stage", END)
        compiled = graph.compile()
        result = owner.run(lambda: compiled.invoke({"next_action": "stage"}))
        assert result["next_action"] == str(plan.owner_thread)


@pytest.mark.parametrize("failure", [CancelledError("user"), RuntimeError("stage failed")])
def test_exception_returns_after_owner_stage_cleanup(failure):
    owner = OwnerCalls()
    events = []

    def stage(route):
        def execute():
            try:
                raise failure
            finally:
                events.append(("cleaned", threading.get_ident()))

        return owner.call(execute)

    graph = StateGraph(Route).add_node("stage", stage)
    graph.add_edge(START, "stage").add_edge("stage", END)
    compiled = graph.compile()
    with pytest.raises(type(failure), match=str(failure)) as error:
        owner.run(lambda: compiled.invoke({"next_action": "stage"}))
    assert error.value is failure
    assert events == [("cleaned", owner.owner_thread)]
    with pytest.raises(RuntimeError, match="owner_bridge_closed"):
        owner.call(lambda: events.append("late-write"))


def test_existing_cancel_token_reaches_active_owner_callback():
    owner = OwnerCalls()
    token = CancellationToken()
    entered = threading.Event()

    def cancel():
        assert entered.wait(5)
        token.cancel("user")

    def stage(route):
        def execute():
            entered.set()
            assert cancelled.wait(5)
            token.check()

        owner.call(execute)
        raise AssertionError("cancelled stage continued")

    cancelled = threading.Event()
    token.add_callback(lambda *_: cancelled.set())
    graph = StateGraph(Route).add_node("stage", stage)
    graph.add_edge(START, "stage").add_edge("stage", END)
    compiled = graph.compile()
    canceller = threading.Thread(target=cancel)
    canceller.start()
    try:
        with pytest.raises(CancelledError, match="user"):
            owner.run(lambda: compiled.invoke({"next_action": "stage"}))
    finally:
        canceller.join(timeout=5)
    assert not canceller.is_alive()


def test_sync_bridge_works_inside_existing_async_caller():
    owner = OwnerCalls()

    def stage(route):
        return {"next_action": str(owner.call(threading.get_ident))}

    graph = StateGraph(Route).add_node("stage", stage)
    graph.add_edge(START, "stage").add_edge("stage", END)
    compiled = graph.compile()

    async def caller():
        return owner.run(lambda: compiled.invoke({"next_action": "stage"}))

    assert asyncio.run(caller())["next_action"] == str(owner.owner_thread)


def test_bridge_rejects_reentrant_run_instead_of_starting_another_owner():
    owner = OwnerCalls()
    with pytest.raises(RuntimeError, match="owner_bridge_already_running"):
        owner.run(lambda: owner.call(lambda: owner.run(lambda: None)))
