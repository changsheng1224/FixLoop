from __future__ import annotations

import pytest

from agent_runtime.config import AgentConfig
from agent_runtime.model_turn import (
    FinishKind,
    ModelTurnResult,
    ProviderFinish,
    ToolCall,
)
from agent_runtime.runtime import Agent
from agent_runtime.step_guard import StepContext, StepGuard
from agent_runtime.tool_executor import QuotaEnforcer
from src.repair.loop_policy import tool_target_paths
from agent_runtime.workspace import WorkspaceContext


def test_apply_patch_recovery_extracts_exact_envelope_paths():
    patch = """*** Begin Patch
*** Update File: django/contrib/auth/validators.py
@@
-old
+new
*** End Patch"""
    assert tool_target_paths("apply_patch", {"patch": patch}) == [
        "django/contrib/auth/validators.py"
    ]


def test_apply_patch_recovery_extracts_all_unique_paths():
    patch = """*** Begin Patch
*** Update File: a.py
@@
-old
+new
*** Add File: b.py
+created = True
*** Delete File: c.py
*** End Patch"""
    assert tool_target_paths("apply_patch", {"patch": patch}) == [
        "a.py",
        "b.py",
        "c.py",
    ]


def test_apply_patch_recovery_does_not_fallback_to_wildcard_on_invalid_patch():
    assert tool_target_paths("apply_patch", {"patch": "not a patch"}) == []


def test_targeted_read_reserve_matches_absolute_model_path():
    quota = QuotaEnforcer()
    quota.grant_read_reserve("django/contrib/auth/validators.py", kind="targeted")
    reserve = quota.matching_read_reserve(
        "read_file",
        {},
        {
            "path": r"C:\workspace\django\contrib\auth\validators.py",
        },
    )
    assert reserve is not None
    assert reserve["path"] == "django/contrib/auth/validators.py"


def test_targeted_read_reserve_does_not_match_similar_filename():
    quota = QuotaEnforcer()
    quota.grant_read_reserve("pkg/a.py", kind="targeted")
    assert quota.matching_read_reserve("read_file", {}, {"path": "pkg/not_a.py"}) is None


class ScriptedNativeClient:
    def __init__(self, results: list[ModelTurnResult]):
        self.results = list(results)
        self.requests = []

    def complete_turn(self, request):
        self.requests.append(request)
        return self.results.pop(0)


def _truncated(text: str = "partial", *, with_tool: bool = False) -> ModelTurnResult:
    calls = [ToolCall("read_file", {"path": "README.md"}, "partial-call")] if with_tool else []
    content = [{"type": "text", "text": text}]
    if with_tool:
        content.append(
            {
                "type": "tool_use",
                "id": "partial-call",
                "name": "read_file",
                "input": {"path": "README.md"},
            }
        )
    return ModelTurnResult(
        text=text,
        tool_calls=calls,
        content=content,
        finish=ProviderFinish(FinishKind.MAX_OUTPUT_TOKENS, "max_tokens", "test"),
        usage={"output_tokens": 512},
    )


def _thinking_truncated() -> ModelTurnResult:
    return ModelTurnResult(
        text="",
        content=[{"type": "thinking", "thinking": "unfinished"}],
        finish=ProviderFinish(FinishKind.MAX_OUTPUT_TOKENS, "max_tokens", "test"),
        usage={"output_tokens": 512},
    )


def _final(text: str = "done") -> ModelTurnResult:
    return ModelTurnResult(
        text=text,
        content=[{"type": "text", "text": text}],
        finish=ProviderFinish(FinishKind.TEXT_COMPLETE, "end_turn", "test"),
    )


def _native_loop(tmp_path, results: list[ModelTurnResult], *, patcher: bool = True):
    """Build an AgentLoop through the same injection path as production.

    Repair behaviour is owned by the injected L2 policy, not by the agent
    name: the canonical ``create_repair_agent`` path injects
    ``RepairLoopPolicy`` for the patcher role.  Tests mirror that here so a
    post-hoc ``_agent_name`` swap is never needed.
    """
    from agent_runtime.agent_loop import AgentLoop
    from agent_runtime.tool_context import ToolContext
    from src.repair.loop_policy import RepairLoopPolicy
    from src.tools.composite import build_repair_agent_tools

    client = ScriptedNativeClient(results)
    ctx = ToolContext(root=str(tmp_path))
    tools = build_repair_agent_tools(ctx, "patcher") if patcher else None
    agent = Agent(
        config=AgentConfig(
            provider="fake", max_steps=3, max_new_tokens=512, approval="auto"
        ),
        model_client=client,
        workspace=WorkspaceContext.build(str(tmp_path)),
        cwd=str(tmp_path),
        tools=tools,
        tool_context=ctx if patcher else None,
        loop_policy=RepairLoopPolicy() if patcher else None,
        agent_name="patcher" if patcher else "",
    )
    loop = AgentLoop(agent)
    events = []
    loop._emit = lambda name, payload=None: events.append((name, payload or {}))
    return loop, agent, client, events


def test_truncated_native_content_is_discarded_before_recovery(tmp_path):
    secret_fragment = "TRUNCATED_FRAGMENT_MUST_NOT_RETURN"
    loop, _agent, client, events = _native_loop(tmp_path, [_truncated(secret_fragment), _final()])

    assert loop.run("fix issue", skip_plan=True) == "done"
    second_messages = client.requests[1].messages
    assert secret_fragment not in str(second_messages)
    assert "[OUTPUT RECOVERY]" in str(second_messages)
    truncated_events = [payload for name, payload in events if name == "model_output_truncated"]
    assert truncated_events == [
        {
            "step": 1,
            "requested_max_output_tokens": 512,
            "actual_output_tokens": 512,
            "text_chars": len(secret_fragment),
            "content_block_count": 1,
            "content_block_counts": {"text": 1},
            "tool_call_count": 0,
            "recovery_attempt": 1,
            "history_action": "discarded",
        }
    ]
    assert secret_fragment not in str(truncated_events)


def test_partial_tool_call_at_max_tokens_is_not_executed(tmp_path):
    loop, agent, _client, _events = _native_loop(
        tmp_path, [_truncated("partial tool", with_tool=True), _final()]
    )

    assert loop.run("fix issue", skip_plan=True) == "done"
    assert agent.session.get("tool_observations", []) == []
    assert not any(item.get("tool_name") == "read_file" for item in agent.session["history"])


def test_second_truncation_has_deterministic_terminal_reason(tmp_path):
    loop, _agent, _client, events = _native_loop(
        tmp_path, [_truncated("first"), _truncated("second")]
    )

    answer = loop.run("fix issue", skip_plan=True)
    assert "连续被截断" in answer
    assert "needs_more_context" in answer
    assert str(loop.stop_reason) == "model_output_truncated"
    assert len([name for name, _payload in events if name == "model_output_truncated"]) == 2


@pytest.mark.parametrize(
    "kinds",
    [
        (FinishKind.EMPTY_OUTPUT, FinishKind.EMPTY_OUTPUT),
        (FinishKind.EMPTY_OUTPUT, FinishKind.MAX_OUTPUT_TOKENS),
        (FinishKind.MAX_OUTPUT_TOKENS, FinishKind.EMPTY_OUTPUT),
    ],
)
def test_empty_and_truncated_outputs_share_one_recovery_attempt(tmp_path, kinds):
    results = [
        _truncated() if kind == FinishKind.MAX_OUTPUT_TOKENS else ModelTurnResult()
        for kind in kinds
    ]
    loop, agent, client, events = _native_loop(tmp_path, [*results, _final("unused")])

    answer = loop.run("fix issue", skip_plan=True)

    assert len(client.requests) == 2
    assert "unused" not in answer
    assert loop._task_state.status == "failed"
    assert loop.stop_reason == (
        "model_output_truncated" if kinds[-1] == FinishKind.MAX_OUTPUT_TOKENS else "parse_fail"
    )
    assert not any(item["role"] == "assistant" for item in agent.session["history"])
    names = [name for name, _ in events]
    assert names.count("run_terminal") == names.count("run_finished") == 1


def test_scoped_read_reserve_is_one_use_and_does_not_open_other_paths():
    quota = QuotaEnforcer(group_limits={"read": 1, "write": 1, "verify": 0, "recovery": 0})
    spec = {"budget_group": "read"}
    quota.record("read_file", spec, {"path": "a.py"})
    assert not quota.check("read_file", spec, {"path": "b.py"})

    assert quota.grant_read_reserve("b.py", kind="post_lock")
    assert quota.check("read_file", spec, {"path": "b.py"})
    assert not quota.check("read_file", spec, {"path": "c.py"})
    quota.record("read_file", spec, {"path": "b.py"})
    assert not quota.check("read_file", spec, {"path": "b.py"})


def test_exact_targeted_reserve_takes_precedence_over_wildcard():
    quota = QuotaEnforcer(group_limits={"read": 0})
    spec = {"budget_group": "read"}
    assert quota.grant_read_reserve("*", kind="targeted")
    assert quota.grant_read_reserve("pkg/a.py", kind="targeted")

    assert quota.check("read_file", spec, {"path": "pkg/a.py"})
    assert not quota.check("read_file", spec, {"path": "pkg/b.py"})


def test_post_lock_reserve_only_allows_exact_read_file_and_returns_receipt():
    quota = QuotaEnforcer(group_limits={"read": 0, "write": 1, "verify": 0, "recovery": 0})
    spec = {"budget_group": "read"}
    assert quota.grant_read_reserve("pkg/a.py", kind="post_lock", generation=3)

    assert quota.check("read_file", spec, {"path": "pkg/a.py"})
    assert not quota.check("grep", spec, {"path": "pkg/a.py", "pattern": "x"})
    assert not quota.check("read_file", spec, {"path": "pkg/b.py"})
    consumed = quota.record("read_file", spec, {"path": "pkg/a.py"}, succeeded=True)
    assert consumed == {"path": "pkg/a.py", "kind": "post_lock", "generation": 3}
    assert not quota.check("read_file", spec, {"path": "pkg/a.py"})


def test_failed_reserved_read_does_not_consume_capability():
    quota = QuotaEnforcer(group_limits={"read": 0})
    spec = {"budget_group": "read"}
    quota.grant_read_reserve("a.py", kind="post_lock", generation=1)

    assert quota.record("read_file", spec, {"path": "a.py"}, succeeded=False) is None
    assert quota.check("read_file", spec, {"path": "a.py"})


def test_novel_reads_are_progress_and_six_reads_enter_convergence():
    guard = StepGuard(stall_threshold=3, reads_before_converge=6)
    guard.reset("fix issue")

    for index in range(1, 6):
        args = {"path": "module.py", "start": index * 10, "end": index * 10 + 5}
        verdict = guard.evaluate(
            StepContext(
                tool_name="read_file",
                tool_args=args,
                progress_key=guard.read_progress_key("read_file", args),
            )
        )
        assert verdict is None
        assert guard.stall_count == 0

    args = {"path": "module.py", "start": 60, "end": 65}
    verdict = guard.evaluate(
        StepContext(
            tool_name="read_file",
            tool_args=args,
            progress_key=guard.read_progress_key("read_file", args),
        )
    )
    assert verdict is not None and verdict.action == "enter_convergence"
    assert guard.phase == "converge"


def test_duplicate_overlap_is_blocked_then_only_one_targeted_read_is_allowed():
    guard = StepGuard()
    guard.reset("fix issue")
    first = {"path": "module.py", "start": 20, "end": 80}
    guard.evaluate(
        StepContext(
            tool_name="read_file",
            tool_args=first,
            progress_key=guard.read_progress_key("read_file", first),
        )
    )

    duplicate = guard.preflight("read_file", {"path": "module.py", "start": 50, "end": 90})
    assert duplicate is not None and duplicate.action == "block_duplicate_read"
    targeted = guard.preflight("read_file", {"path": "other.py", "start": 1, "end": 20})
    assert targeted is not None and targeted.action == "allow_targeted_read"
    blocked = guard.preflight("grep", {"path": ".", "pattern": "another"})
    assert blocked is not None and blocked.action == "block_convergence_read"


def test_stale_recovery_reopens_targeted_read_after_normal_reserve_used():
    guard = StepGuard()
    guard.reset("fix issue")
    guard.enter_convergence("read_limit")
    first = guard.preflight("read_file", {"path": "a.py", "start": 1, "end": 10})
    assert first is not None and first.action == "allow_targeted_read"

    assert guard.request_targeted_reread("stale_preimage")
    second = guard.preflight("read_file", {"path": "a.py", "start": 11, "end": 20})
    assert second is not None and second.action == "allow_targeted_read"


def test_post_lock_read_precedes_duplicate_and_convergence_gate():
    guard = StepGuard()
    guard.reset("fix issue")
    args = {"path": "module.py", "start": 20, "end": 80}
    guard.evaluate(
        StepContext(
            tool_name="read_file",
            tool_args=args,
            progress_key=guard.read_progress_key("read_file", args),
        )
    )
    guard.enter_convergence("test")

    verdict = guard.preflight(
        "read_file",
        args,
        read_reservation={"path": "module.py", "kind": "post_lock", "generation": 2},
    )
    assert verdict is not None and verdict.action == "allow_reserved_read"


def test_thinking_only_recovery_uses_patch_decision_tools_and_smaller_budget(tmp_path):
    loop, agent, client, events = _native_loop(
        tmp_path, [_thinking_truncated(), _final("cannot_patch: insufficient evidence")]
    )

    loop.run("fix issue", skip_plan=True)

    assert client.requests[1].max_output_tokens <= 4096
    second_tools = {item["name"] for item in client.requests[1].tools}
    assert {"apply_patch", "patch_file", "finish_repair"} <= second_tools
    assert "read_file" not in second_tools
    assert "grep" not in second_tools
    assert client.requests[1].tool_choice is not None
    assert client.requests[1].tool_choice.mode == "required"
    second_prompt = str(client.requests[1].messages[0]["content"])
    assert second_prompt.startswith("[OUTPUT RECOVERY]")
    assert "[PATCH DECISION REQUIRED]" not in second_prompt
    assert any(name == "thinking_only_truncation" for name, _ in events)


def test_patch_decision_gate_filters_reads_at_request_projection(tmp_path):
    loop, agent, _client, _events = _native_loop(tmp_path, [_final()])
    loop._step_guard.enter_convergence("read_limit_without_write")
    loop._tool_state.action_required = True

    names = loop.agent.loop_policy.visible_tools(loop._policy_context(), action_required=True)

    assert names == {"apply_patch", "patch_file", "finish_repair"}


def test_reading_editable_implementation_syncs_grounding_and_forces_patch(tmp_path):
    from agent_runtime.tool_result import ToolResult
    from src.repair.execution.edit_lock import EditLockState

    target = tmp_path / "module.py"
    target.write_text("value = 1\n", encoding="utf-8")
    loop, agent, _client, events = _native_loop(tmp_path, [_final()])
    from src.state import RepairState

    state = RepairState(issue_input="fix")
    from src.repair.l2_binding import bind_l2_context

    bind_l2_context(
        agent,
        repair_run_id="test",
        agent_name="patcher",
        phase="patch",
        attempt=0,
        repair_state=state,
    )
    lock = EditLockState(repo_root=tmp_path, allowed_edit=set())
    agent.tool_context.edit_lock = lock
    try:
        lock.mark_read("module.py", auto_allow_impl=True)
        result = ToolResult(
            content="module.py", status="success", metadata={}
        )
        loop.agent.loop_policy.sync_grounding(
            loop._policy_context(), "read_file", {"path": "module.py"}, result
        )
        assert state.node_timings["patcher_grounded"] is True
        assert state.node_timings["patch_required"] is True
        assert loop._tool_state.action_required is True
        assert loop._tool_state.recovery_allowed_tools == {
            "apply_patch",
            "patch_file",
            "finish_repair",
        }
        assert any(name == "patcher_grounded" for name, _payload in events)
    finally:
        agent.tool_context.edit_lock = None


def test_grounded_patcher_cannot_finish_with_needs_more_context(tmp_path):
    from agent_runtime.tool_result import ToolResult

    loop, agent, _client, _events = _native_loop(tmp_path, [_final()])
    agent.session["_patcher_runtime"] = {"grounded": True}
    result = ToolResult(content="", status="success", metadata={})

    assert (
        loop.agent.loop_policy.review_result(
            loop._policy_context(),
            "finish_repair",
            {"status": "needs_more_context", "reason": "need more context"},
            result,
        )
        is True
    )
    assert result.error_code == "grounded_finish_blocked"
    assert result.metadata["required_next_action"] == "apply_patch_or_cannot_patch"


def test_patcher_terminal_classifies_no_write_attempt():
    from src.repair.execution.patcher_contract import (
        PatcherTerminalStatus,
        classify_patcher_attempt,
    )
    from src.state import RepairState

    state = RepairState(issue_input="fix")
    state.control.patcher_write_attempted = False
    assert classify_patcher_attempt(state, []) is PatcherTerminalStatus.NO_WRITE_ATTEMPT


def test_targeted_reread_reserve_overrides_patch_decision_for_exact_read(tmp_path):
    loop, agent, _client, _events = _native_loop(tmp_path, [_final()])
    loop._step_guard.enter_convergence("stale_preimage")
    loop._tool_state.action_required = True
    agent.quota.grant_read_reserve("pkg/a.py", kind="targeted")

    names = loop.agent.loop_policy.visible_tools(loop._policy_context(), action_required=True)

    assert "read_file" in names
    assert "grep" not in names
    assert {"apply_patch", "finish_repair"} <= names


def test_patch_recovery_projection_forces_apply_patch_after_invalid_args(tmp_path):
    loop, agent, _client, _events = _native_loop(tmp_path, [_final()])
    loop.agent.loop_policy.set_recovery(
        loop._policy_context(),
        "invalid_args",
        "use grounded apply_patch",
        {"apply_patch", "finish_repair"},
    )

    assert loop.agent.loop_policy.visible_tools(loop._policy_context(), action_required=True) == {
        "apply_patch",
        "finish_repair",
    }


def test_patcher_localization_window_keeps_reads_after_action_gate(tmp_path):
    loop, agent, _client, _events = _native_loop(tmp_path, [_final()])
    loop._step_guard.reset("fix issue", localization_mode=True)
    loop._step_guard.enter_convergence("read_limit_without_write")
    loop._tool_state.action_required = True

    names = loop.agent.loop_policy.visible_tools(loop._policy_context(), action_required=True)

    assert {"read_file", "grep", "apply_patch", "finish_repair"} <= names


def test_duplicate_closes_patcher_localization_window():
    guard = StepGuard()
    guard.reset("fix issue", localization_mode=True)
    guard.enter_convergence("read_limit_without_write")
    args = {"path": "module.py", "start": 1, "end": 10}

    verdict = guard.preflight("read_file", args)

    assert verdict is not None and verdict.action == "allow_localization_read"
    guard.evaluate(
        StepContext(
            tool_name="read_file",
            tool_args=args,
            progress_key=guard.read_progress_key("read_file", args),
        )
    )
    duplicate = guard.preflight("read_file", args)
    assert duplicate is not None and duplicate.action == "block_duplicate_read"
    assert guard.localization_reads_available is False


def test_thinking_only_recovery_accepts_structured_terminal_tool(tmp_path):
    loop, agent, client, events = _native_loop(
        tmp_path,
        [
            _thinking_truncated(),
            ModelTurnResult(
                tool_calls=[
                    ToolCall(
                        "finish_repair",
                        {
                            "status": "needs_more_context",
                            "reason": "target implementation was not present in available evidence",
                        },
                        "finish-call",
                    )
                ],
                content=[
                    {
                        "type": "tool_use",
                        "id": "finish-call",
                        "name": "finish_repair",
                        "input": {
                            "status": "needs_more_context",
                            "reason": "target implementation was not present in available evidence",
                        },
                    }
                ],
                finish=ProviderFinish(FinishKind.TOOL_CALLS, "tool_use", "test"),
            ),
        ],
    )

    answer = loop.run("fix issue", skip_plan=True)

    assert '"status": "needs_more_context"' in answer
    assert client.requests[1].tool_choice.mode == "required"
    assert any(name == "terminal_tool_accepted" for name, _ in events)


def test_finish_repair_has_terminal_reserve_when_recovery_budget_is_exhausted():
    quota = QuotaEnforcer(group_limits={"read": 0, "write": 0, "verify": 0, "recovery": 0})
    spec = {"budget_group": "recovery", "terminal": True}
    args = {"status": "needs_more_context", "reason": "evidence is incomplete"}

    assert quota.check("finish_repair", spec, args)
    assert quota.record("finish_repair", spec, args, succeeded=True) is None
    assert not quota.check("finish_repair", spec, args)
    assert quota.quota_summary()["terminal_reserve"]["used"] is True


def test_successful_recovery_write_returns_to_normal_tool_choice(tmp_path):
    target = tmp_path / "module.py"
    target.write_text("value = 1\n", encoding="utf-8")
    loop, agent, client, _events = _native_loop(
        tmp_path,
        [
            _thinking_truncated(),
            ModelTurnResult(
                tool_calls=[
                    ToolCall(
                        "patch_file",
                        {
                            "path": "module.py",
                            "old_text": "value = 1",
                            "new_text": "value = 2",
                        },
                        "patch-call",
                    )
                ],
                content=[
                    {
                        "type": "tool_use",
                        "id": "patch-call",
                        "name": "patch_file",
                        "input": {
                            "path": "module.py",
                            "old_text": "value = 1",
                            "new_text": "value = 2",
                        },
                    }
                ],
                finish=ProviderFinish(FinishKind.TOOL_CALLS, "tool_use", "test"),
            ),
            _final("patched"),
        ],
    )
    agent.config.approval = "auto"

    assert loop.run("fix issue", skip_plan=True) == "patched"
    assert target.read_text(encoding="utf-8") == "value = 2\n"
    assert client.requests[1].tool_choice.mode == "required"
    assert client.requests[2].tool_choice is None


def test_patcher_default_output_budget_is_8192(tmp_path):
    from agent_runtime.providers.clients import FakeModelClient
    from src.agents.factory import create_patcher

    workspace = WorkspaceContext.build(str(tmp_path))
    patcher = create_patcher(FakeModelClient(["<final>done</final>"]), workspace)
    assert patcher.config.max_new_tokens == 8192
