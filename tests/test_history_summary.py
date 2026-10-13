"""S4 structural guarantees, whole-interaction packing and bounded fallback."""

import json
from copy import deepcopy

import pytest

from agent_runtime.budget_manager import BudgetManager
from agent_runtime.cancellation import CancelledError
from agent_runtime.compression_pipeline import (
    count_history_tokens,
    l5_auto_compact,
    make_summarizer,
    run_compression_pipeline,
)
from agent_runtime.config import AgentConfig
from agent_runtime.context_manager import ContextManager, TokenBudget
from agent_runtime.errors import ContextBuildBlockedError
from agent_runtime.history_summary import (
    SUMMARY_PREFIX,
    build_summary_prompt,
    encode,
    fit_summary,
    interaction_groups,
    is_summary,
    parse_summary,
)
from agent_runtime.message_projection import init_run_projection, seal_history_at_build
from agent_runtime.model_call_budget import ModelBudgetExceededError
from agent_runtime.providers.clients import FakeModelClient
from agent_runtime.runtime import Agent
from agent_runtime.tier_policy import TierPolicy
from agent_runtime.workspace import WorkspaceContext
from tests.history_summary_support import summary_json


@pytest.fixture
def budget():
    return TokenBudget(model="gpt-4", provider="openai", total_limit=6000)


def history(n=15):
    return [{"role": "user", "content": f"request-{i} " + "body " * 25} for i in range(n)]


def payload(prompt):
    return json.loads(prompt.split("\nINPUT_JSON:\n")[1])


def native_group(i):
    return [
        {"role": "user", "content": f"request-{i}"},
        {
            "role": "assistant",
            "content": [
                {
                    "type": "tool_use",
                    "id": f"call-{i}",
                    "name": "read_file",
                    "input": {"path": "a.py"},
                }
            ],
        },
        {
            "role": "user",
            "content": [
                {
                    "type": "tool_result",
                    "tool_use_id": f"call-{i}",
                    "content": "FAILED assertion value == expected",
                }
            ],
        },
        {"role": "assistant", "content": f"inspect failure-{i}"},
    ]


@pytest.mark.parametrize(
    "text",
    [
        "free text",
        '{"confirmed_facts": []',
        summary_json() + " trailing",
        summary_json().replace('"next_action":""', '"next_action":"","next_action":""'),
        summary_json(confirmed_facts="fact"),
        summary_json(hypotheses=[1]),
        summary_json(verification={"status": [], "details": []}),
        summary_json(verification={"status": "unknown", "details": [""]}),
        summary_json(next_action=[]),
        summary_json(extra="not in contract"),
    ],
)
def test_invalid_schema_is_rejected(text):
    with pytest.raises(ValueError):
        parse_summary(text)


def test_refs_are_offered_pointers_only():
    assert parse_summary(summary_json(evidence_refs=["obs-1"]), offered_refs=["obs-1"])
    with pytest.raises(ValueError, match="reference_not_offered"):
        parse_summary(summary_json(evidence_refs=["invented"]), offered_refs=["obs-1"])


def test_output_drops_whole_items_and_remains_json(budget):
    value = parse_summary(
        summary_json(
            "已观察事实" * 200,
            hypotheses=["假设" * 200],
            verification={"status": "failed", "details": ["FAILED assertion value == 1"]},
            evidence_refs=["obs-1"],
        )
    )
    original = deepcopy(value)
    minimal = parse_summary(
        summary_json(verification=value["verification"], evidence_refs=["obs-1"])
    )
    text, dropped = fit_summary(value, budget, budget.count(encode(minimal)))
    assert parse_summary(text) == minimal and dropped == 2
    assert value == original
    with pytest.raises(ValueError, match="minimum_over_budget"):
        fit_summary(value, budget, 1)


def test_input_keeps_full_bodies_failures_refs_and_reports_omissions(budget):
    groups = [native_group(i) for i in range(3)]
    groups[0][2]["observation_id"] = "obs-failure"
    groups[0][2]["content"][0]["content"] += "\n" + "failure detail " * 80
    groups[1][2]["content"][0]["content"] = "ordinary " * 1600
    groups[2][2]["content"][0]["content"] = "ordinary " * 1600
    selected_prompt, _, _ = build_summary_prompt(groups[0], budget, 6000)
    prompt, refs, coverage = build_summary_prompt(
        sum(groups, []), budget, budget.count(selected_prompt) + 30
    )
    data = payload(prompt)
    assert data["interactions"] == [groups[0]]
    assert data["omitted_interactions"] == coverage["omitted_groups"] == 2
    assert refs == ["obs-failure"]
    assert "failure detail " * 80 in prompt
    with pytest.raises(ValueError, match="no_complete_interaction_fits"):
        build_summary_prompt(groups[1], budget, budget.count(selected_prompt) + 30)


def test_multiple_previous_summaries_are_replaced_once(budget):
    previous = [
        {
            "role": "system",
            "content": SUMMARY_PREFIX + summary_json(f"old-{i}", evidence_refs=[f"obs-{i}"]),
        }
        for i in range(2)
    ]
    calls = []

    def summarize(prompt):
        calls.append(payload(prompt))
        return summary_json("merged", evidence_refs=["obs-0", "obs-1"])

    result = l5_auto_compact(
        previous + history(), budget, {}, summarizer=summarize, trigger_tokens=1
    )
    assert len([i for i in result if is_summary(i)]) == 1
    assert len(calls[0]["previous_summaries"]) == 2
    second = l5_auto_compact(
        result + history(4), budget, {}, summarizer=summarize, trigger_tokens=1
    )
    assert len([i for i in second if is_summary(i)]) == 1
    assert len(calls[1]["previous_summaries"]) == 1
    assert calls[1]["previous_summaries"][0]["confirmed_facts"] == ["merged"]


@pytest.mark.parametrize("raw", ["legacy prose", summary_json(evidence_refs=["invented"])])
def test_cache_and_fresh_output_use_same_validation_without_cache_mutation(budget, raw):
    cache, first_meta, second_meta = {}, {}, {}
    l5_auto_compact(
        history(),
        budget,
        first_meta,
        summarizer=lambda _: raw,
        summary_cache=cache,
        trigger_tokens=1,
    )
    before = dict(cache)

    def should_not_call(_):
        pytest.fail("cache hit must not call provider")

    result = l5_auto_compact(
        history(),
        budget,
        second_meta,
        summarizer=should_not_call,
        summary_cache=cache,
        trigger_tokens=1,
    )
    assert cache == before
    assert second_meta["compression_pipeline"]["l5_summary_cache_hit"]
    assert second_meta["compression_pipeline"]["l5_fallback"]
    assert (
        first_meta["compression_pipeline"]["l5_failure_reason"]
        == second_meta["compression_pipeline"]["l5_failure_reason"]
    )
    assert not any(is_summary(i) for i in result)


@pytest.mark.parametrize(
    "error",
    [RuntimeError("offline"), TimeoutError("timeout"), ValueError("model_call_budget_exhausted")],
)
def test_failure_fallback_keeps_complete_native_interactions_and_state(budget, error):
    groups = [native_group(i) for i in range(12)]
    original = sum(groups, []) + [
        {"role": "system", "repair_state": True, "content": "original goal and active constraints"}
    ]
    before = deepcopy(original)

    def fail(_):
        raise error

    meta = {}
    result = l5_auto_compact(
        original, budget, meta, summarizer=fail, trigger_tokens=1, history_window=6000
    )
    assert original == before
    assert result[-2]["repair_state"] or any(i.get("repair_state") for i in result)
    assert groups[-1] == [i for i in result if i in groups[-1]]
    for group in groups:
        assert all(i in result for i in group) or all(i not in result for i in group)
    assert count_history_tokens(result, budget) <= 6000
    assert any(i.get("_history_omission") for i in result)


def test_cancellation_propagates_and_protected_overflow_blocks(budget):
    def cancelled(_):
        raise CancelledError("stop")

    with pytest.raises(CancelledError):
        l5_auto_compact(history(), budget, {}, summarizer=cancelled, trigger_tokens=1)
    for protected in [
        {"role": "system", "repair_state": True},
        {"role": "user", "turn_id": 99},
    ]:
        original = history() + [{**protected, "content": "unalterable " * 1000}]
        with pytest.raises(ContextBuildBlockedError, match="history_protected_over_budget"):
            l5_auto_compact(
                original, budget, {}, trigger_tokens=1, history_window=400, current_turn_id=99
            )


@pytest.mark.parametrize("openai", [False, True])
def test_native_calls_remain_paired_through_pipeline(budget, openai):
    if openai:
        group = [
            {"role": "user", "content": "inspect"},
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    {
                        "id": "one",
                        "type": "function",
                        "function": {"name": "read_file", "arguments": "{}"},
                    }
                ],
            },
            {
                "role": "tool",
                "tool_call_id": "one",
                "tool_name": "read_file",
                "content": "Error: 不在允许列表中",
            },
        ]
    else:
        group = native_group(0)
    original = group + history()
    before = deepcopy(original)
    result = run_compression_pipeline(
        original,
        budget,
        metadata={},
        tier_policy=TierPolicy(allowed_tools=frozenset({"grep"})),
        l5_trigger_tokens=1,
    )
    assert original == before
    calls = [i for i in result if i.get("tool_calls") or isinstance(i.get("content"), list)]
    assert calls
    assert interaction_groups(result)


def test_mismatched_native_history_blocks_and_user_text_is_not_summary():
    assert not is_summary({"role": "user", "content": SUMMARY_PREFIX + summary_json()})
    with pytest.raises(ContextBuildBlockedError, match="history_tool_pair_mismatch"):
        interaction_groups(native_group(0)[:2])
    with pytest.raises(ContextBuildBlockedError, match="history_tool_pair_mismatch"):
        interaction_groups(native_group(0)[2:])


def agent_at(tmp_path):
    return Agent(
        config=AgentConfig(provider="fake", prompt_budget=6000),
        model_client=FakeModelClient([summary_json("历史事实 " * 120)]),
        workspace=WorkspaceContext.build(str(tmp_path)),
        cwd=str(tmp_path),
    )


def test_summarizer_retains_long_json_and_charges_shared_budget(tmp_path):
    agent = agent_at(tmp_path)
    agent.budget_manager = BudgetManager({"llm_calls": 1})
    raw = make_summarizer(agent)("summarize")
    assert len(raw) > 300 and parse_summary(raw)
    call = next(iter(agent.budget_manager.snapshot()["model_calls"].values()))
    assert call["purpose"] == "summary" and call["usage_status"] == "actual"
    meta = {}
    result = l5_auto_compact(
        history(),
        TokenBudget(total_limit=6000),
        meta,
        summarizer=make_summarizer(agent),
        trigger_tokens=1,
    )
    assert meta["compression_pipeline"]["l5_failure_reason"] == "model_call_budget_exhausted"
    assert not any(is_summary(i) for i in result)
    assert len(agent.model_client.prompts) == 1


@pytest.mark.parametrize("fails", [False, True])
def test_summary_unknown_usage_is_conservative_and_not_refunded(tmp_path, fails):
    agent = agent_at(tmp_path)

    class NoUsageClient:
        calls = 0

        def complete(self, prompt, **kwargs):
            self.calls += 1
            if fails:
                raise TimeoutError("timed out without usage")
            return summary_json("historical fact")

    agent.light_client = NoUsageClient()
    agent.budget_manager = BudgetManager({"llm_calls": 1})
    meta = {}
    l5_auto_compact(
        history(),
        TokenBudget(total_limit=6000),
        meta,
        summarizer=make_summarizer(agent),
        trigger_tokens=1,
    )
    snapshot = agent.budget_manager.snapshot()
    call = next(iter(snapshot["model_calls"].values()))
    assert call["purpose"] == "summary"
    assert call["usage_status"] == ("unknown" if fails else "estimated")
    restored = BudgetManager()
    restored.restore(snapshot)
    agent.budget_manager = restored
    with pytest.raises(ModelBudgetExceededError):
        make_summarizer(agent)("try again")
    assert agent.light_client.calls == 1


def test_final_history_fitter_keeps_json_and_interactions_atomic(tmp_path):
    manager = ContextManager(agent_at(tmp_path))
    marker = {"role": "system", "content": SUMMARY_PREFIX + summary_json("历史事实 " * 120)}
    text = manager._format_compressed_result([marker])
    assert marker["content"] in text and len(text) > 300
    groups = [manager._format_compressed_result(native_group(i)) for i in range(3)]
    meta = {"_history_units": [text, *groups]}
    fitted = manager._fit_history_text(
        "\n".join(meta["_history_units"]), manager.budget.count(groups[-1]) + 35, meta
    )
    assert meta["compression_pipeline"]["history_omitted_units"] > 0
    assert "History omitted" in fitted
    for group in [text, *groups]:
        assert group in fitted or all(
            line not in fitted
            for line in group.splitlines()
            if "call-" in line or SUMMARY_PREFIX in line
        )


def test_sealed_history_is_an_atomic_unit(tmp_path):
    agent = agent_at(tmp_path)
    agent.record({"role": "user", "content": "first"})
    init_run_projection(agent.session, "task")
    sealed = "SEALED " * 1000
    seal_history_at_build(agent.session, 1, sealed)
    agent.record({"role": "user", "content": "recent-whole-interaction"})
    manager, meta = ContextManager(agent), {}
    text = manager._get_compressed_history(meta)
    fitted = manager._fit_history_text(text, 100, meta)
    assert "SEALED" not in fitted and "recent-whole-interaction" in fitted
    assert agent.session["_run_projection"].sealed_history_text == sealed


def test_sealed_summary_prevents_another_summary_without_rewriting_prefix(tmp_path, monkeypatch):
    agent = agent_at(tmp_path)
    agent.record({"role": "user", "content": "first"})
    init_run_projection(agent.session, "task")
    sealed = "**system**: " + SUMMARY_PREFIX + summary_json("first summary")
    seal_history_at_build(agent.session, 1, sealed)
    for entry in history():
        agent.record(entry)
    from agent_runtime import context_manager

    original = context_manager.run_compression_pipeline

    def force_l5(*args, **kwargs):
        kwargs["l5_trigger_tokens"] = 1
        return original(*args, **kwargs)

    monkeypatch.setattr(context_manager, "run_compression_pipeline", force_l5)
    meta = {}
    text = ContextManager(agent)._get_compressed_history(meta)
    assert text.startswith(sealed) and text.count(SUMMARY_PREFIX) == 1
    assert meta["compression_pipeline"]["l5_failure_reason"] == "summary_sealed_prefix_retained"
    assert agent.model_client.prompts == []


def test_formatted_openai_calls_include_ids_and_arguments(tmp_path):
    manager = ContextManager(agent_at(tmp_path))
    call = {
        "role": "assistant",
        "content": "",
        "tool_calls": [
            {"id": "call-1", "function": {"name": "grep", "arguments": '{"pattern":"ValueError"}'}}
        ],
    }
    result = {"role": "tool", "tool_call_id": "call-1", "content": "found ValueError"}
    text = manager._format_split_history([call, result], True)
    assert encode(call) in text
