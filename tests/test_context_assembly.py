"""Elastic selection and final requests, including actual runtime calls."""

import hashlib
import json
from types import SimpleNamespace

import pytest

from agent_runtime.checkpoint import create_checkpoint, evaluate_resume_state
from agent_runtime.context_manager import ContextManager
from agent_runtime.context_preparation import native_groups
from agent_runtime.errors import ContextBuildBlockedError
from agent_runtime.section_filler import SectionFiller
from agent_runtime.task_state import TaskState
from tests.plan_support import session_for, simple_plan, through_analysis, write
from tests.test_required_long_task_context import _agent


class CharBudget:
    count = staticmethod(len)

    @staticmethod
    def fit(text, limit):
        return text[:limit]


def test_unused_soft_quotas_are_lent_without_increasing_total():
    metadata = {"cuts": [], "sections": {}}
    filler = SectionFiller(
        CharBudget(),
        metadata,
        section_cap=1100,
        total_limit=1100,
        scaled_budget=lambda limit, total: limit,
    )
    filler.add_required({"state": "R" * 100})
    filler.add_elastic(
        {"source": "S" * 900, "feedback": "F" * 100},
        {"source": 60, "feedback": 30, "history": 10},
        ["source", "feedback", "history"],
    )
    assert filler.sections["state"] == "R" * 100
    assert len(filler.sections["source"]) == 900
    assert len(filler.sections["feedback"]) == 100
    assert filler.used == 1100
    elastic = metadata["elastic_budget"]
    assert elastic["allocations"]["source"]["borrowed_tokens"] == 300
    assert elastic["unused_tokens"] == 0


@pytest.mark.parametrize("pool", [0, 97, 1000, 4096])
def test_elastic_accounting_is_bounded_and_deterministic(pool):
    outcomes = []
    for _ in range(2):
        metadata = {"cuts": [], "sections": {}}
        filler = SectionFiller(
            CharBudget(),
            metadata,
            section_cap=pool,
            total_limit=pool,
            scaled_budget=lambda limit, total: limit,
        )
        filler.add_elastic(
            {"source": "a" * 8000, "history": "b" * 700},
            {"source": 60, "history": 40},
            ["source", "history"],
        )
        assert filler.used <= pool
        assert filler.used == sum(metadata["sections"].values())
        assert filler.used + metadata["elastic_budget"]["unused_tokens"] == pool
        outcomes.append((filler.sections, metadata))
    assert outcomes[0] == outcomes[1]


def _group(call_id, content):
    return [
        {
            "role": "assistant",
            "content": [
                {
                    "type": "tool_use",
                    "id": call_id,
                    "name": "read_file",
                    "input": {"path": "value.py"},
                }
            ],
        },
        {
            "role": "user",
            "content": [{"type": "tool_result", "tool_use_id": call_id, "content": content}],
        },
    ]


@pytest.mark.parametrize("damage", ["dangling", "mismatch", "duplicate", "block", "id", "role"])
def test_unpaired_native_messages_are_rejected_even_when_over_budget(damage):
    group = _group("call", "large " * 10000)
    if damage == "dangling":
        group = group[:1]
    elif damage == "mismatch":
        group[1]["content"][0]["tool_use_id"] = "other"
    elif damage == "duplicate":
        group *= 2
    elif damage == "block":
        group[0]["content"] = ["invalid block"]
    elif damage == "id":
        group[0]["content"][0]["id"] = ["invalid id"]
    else:
        group[0] = "invalid message"
    with pytest.raises(ContextBuildBlockedError, match="context_tool_pair_mismatch"):
        native_groups(group)


def test_native_tail_selects_complete_recent_groups_under_shared_budget(tmp_path):
    (tmp_path / "value.py").write_text("value = 1\n")
    with session_for(tmp_path) as session:
        session.create(simple_plan(session))
        session.configure_long_task("task", hard_constraints=["preserve tests"])
        agent, _ = _agent(tmp_path, session, native=True, budget=1800)
        manager = ContextManager(agent)
        manager._get_workspace = lambda: ""
        manager._get_memory = lambda: ""
        manager._get_knowledge = lambda query: ""
        manager._get_compressed_history = lambda metadata: ""
        tail = [*_group("large", "old payload " * 10000), *_group("small", "current feedback")]
        request, metadata = manager.prepare_request("continue", protocol="native", native_tail=tail)
        assert len(native_groups(request.messages[1:])) == 1
        assert metadata["selected_tool_call_ids"] == ["small"]
        assert metadata["dropped_tool_call_ids"] == ["large"]
        assert metadata["provider_input_tokens"] <= 1800
        assert "current feedback" in str(request.messages)
        assert "old payload" not in str(request.messages)


def test_verify_node_prioritizes_feedback_over_source(tmp_path):
    (tmp_path / "value.py").write_text("value = 1\n")
    with session_for(tmp_path) as session:
        session.create(simple_plan(session))
        session.configure_long_task("fix value")
        through_analysis(session)
        session.run_node("edit", lambda attempt: write(session, attempt))
        agent, _ = _agent(tmp_path, session)
        _, metadata = ContextManager(agent).prepare_request("verify", protocol="xml")
        elastic = metadata["elastic_budget"]
        assert elastic["priority"].index("feedback") < elastic["priority"].index("source")
        assert metadata["required_state_ref"]["node_id"] == "verify"


@pytest.mark.parametrize("native", [False, True])
def test_final_manifest_matches_actual_request_and_checkpoint(tmp_path, native):
    (tmp_path / "value.py").write_text("value = 1\n")
    with session_for(tmp_path) as session:
        session.create(simple_plan(session))
        session.configure_long_task("原始目标", hard_constraints=["保留约束全文"])
        agent, client = _agent(tmp_path, session, native=native)
        assert agent.ask("继续", skip_plan=True) == "done"
        manifest = agent.session["context_manifest"]
        if native:
            actual = client.requests[0]
            payload = json.dumps(
                {
                    "system": actual.system_prompt,
                    "messages": actual.messages,
                    "tools": actual.tools,
                    "tool_choice": None,
                    "max_output_tokens": actual.max_output_tokens,
                },
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
        else:
            payload = client.prompts[0]
        assert manifest["request_hash"] == hashlib.sha256(payload.encode()).hexdigest()
        assert manifest["stage"] == "prepared"
        assert manifest["provider_input_tokens"] <= agent.config.prompt_budget
        assert manifest["elastic_budget"]
        checkpoint = create_checkpoint(agent, TaskState.create(user_request="continue"), "continue")
        assert checkpoint["context_manifest"]["request_hash"] == manifest["request_hash"]
        assert checkpoint["context_manifest"]["elastic_budget"] == manifest["elastic_budget"]
        assert checkpoint["context_manifest"]["stage"] == "prepared"
        saved = checkpoint["context_manifest"]["elastic_budget"]["allocations"]["source"]
        manifest["elastic_budget"]["allocations"]["source"]["reason"] = "changed after checkpoint"
        assert saved["reason"] != "changed after checkpoint"
        assert evaluate_resume_state(agent)["status"] == "full-valid"


@pytest.mark.parametrize("native", [False, True])
def test_callback_cannot_change_a_prepared_request_before_model_call(tmp_path, native, monkeypatch):
    (tmp_path / "value.py").write_text("value = 1\n")
    with session_for(tmp_path) as session:
        session.create(simple_plan(session))
        session.configure_long_task("task")
        agent, client = _agent(tmp_path, session, native=native)
        original = ContextManager.prepare_request
        holder = {}

        def capture(self, *args, **kwargs):
            request, metadata = original(self, *args, **kwargs)
            holder["request"] = request
            return request, metadata

        def tamper(**kwargs):
            holder["request"].messages[0]["content"] = "different request"

        monkeypatch.setattr(ContextManager, "prepare_request", capture)
        answer = agent.ask(
            "continue", callback=SimpleNamespace(on_pre_model=tamper), skip_plan=True
        )
        assert "context_request_changed" in answer
        assert not client.prompts
        if native:
            assert not client.requests


def test_directives_are_protected_and_explicit_protocol_reserve_has_no_agent_scratch(tmp_path):
    (tmp_path / "value.py").write_text("value = 1\n")
    with session_for(tmp_path) as session:
        session.create(simple_plan(session))
        session.configure_long_task("task")
        agent, _ = _agent(tmp_path, session, native=True)
        agent._context_protocol_tokens = 100000
        request, metadata = ContextManager(agent).prepare_request(
            "continue", protocol="native", directives=["[OUTPUT RECOVERY] retain this rule"]
        )
        assert "retain this rule" in request.messages[0]["content"]
        assert metadata["protocol_reserved_tokens"] < 100000
        with pytest.raises(ContextBuildBlockedError, match="context_required_over_budget"):
            ContextManager(agent).prepare_request(
                "continue", protocol="native", directives=["retain every rule " * 10000]
            )


def test_real_native_read_preserves_pairing_and_final_hash(tmp_path):
    (tmp_path / "value.py").write_text("value = 1\n")
    with session_for(tmp_path) as session:
        session.create(simple_plan(session))
        session.configure_long_task("inspect", hard_constraints=["do not edit"])
        agent, client = _agent(tmp_path, session, native=True)
        agent.shared_run_id = session.identity["run_id"]
        client._outputs = [
            '<tool>{"name":"read_file","args":{"path":"value.py"}}</tool>',
            "<final>done</final>",
        ]

        def invoke(attempt):
            assert agent.ask("inspect", skip_plan=True) == "done"
            return session.tool_result(attempt)

        assert session.run_node("read-0", invoke)["status"] == "success"
        request = client.requests[-1]
        assert len(native_groups(request.messages[1:])) == 1
        assert agent.session["context_manifest"]["selected_tool_call_ids"]
        assert "value = 1" in str(request.messages)
        assert len(session.store.latest("operation", "operation_id")) == 1


def test_large_fresh_source_can_use_reclaimed_budget(tmp_path):
    from agent_runtime.code_exploration.context import select_source_context
    from agent_runtime.context_manager import TokenBudget
    from tests.test_code_relations import _observe, _setup

    root, state, context, service = _setup(tmp_path, "test_relation")
    (root / "large.py").write_text('def large():\n    return "' + "word " * 2000 + '"\n')
    try:
        _observe(state, context, service, "large.py")
        service.relations({})
        budget = TokenBudget(provider="fake")
        legacy, _ = select_source_context(
            service, budget, role="", phase="repair", token_limit=4000
        )
        elastic, selection = select_source_context(
            service, budget, role="", phase="repair", token_limit=4000, elastic=True
        )
        assert "word word" not in legacy
        assert "word word" in elastic and selection.selected
        assert budget.count(elastic) > 1500
        assert "snippet_freshness=fresh" in elastic
    finally:
        service.close()


def test_final_native_encoding_drops_optional_text_and_records_final_manifest(tmp_path):
    from agent_runtime.context_preparation import request_hash

    (tmp_path / "value.py").write_text("value = 1\n")
    with session_for(tmp_path) as session:
        session.create(simple_plan(session))
        session.configure_long_task("retain goal", hard_constraints=["retain constraint"])
        agent, _ = _agent(tmp_path, session, native=True, budget=1800)
        manager = ContextManager(agent)
        manager._get_workspace = lambda: ""
        manager._get_memory = lambda: '"' * 20000
        manager._get_knowledge = lambda query: ""
        manager._get_compressed_history = lambda metadata: ""
        request, metadata = manager.prepare_request("continue", protocol="native")
        manifest = agent.session["context_manifest"]
        assert "elastic:memory:protocol_budget" in metadata["cuts"]
        assert manifest["elastic_budget"]["allocations"]["memory"]["used_tokens"] == 0
        assert manifest["request_hash"] == request_hash(request, "native")
        assert manifest["provider_input_tokens"] <= 1800
        assert "retain goal" in str(request.messages)
        assert "retain constraint" in str(request.messages)
        assert manager.budget.total_limit == 1800


def test_forced_action_omits_validated_complete_tail_and_records_reason(tmp_path):
    (tmp_path / "value.py").write_text("value = 1\n")
    with session_for(tmp_path) as session:
        session.create(simple_plan(session))
        session.configure_long_task("task")
        agent, _ = _agent(tmp_path, session, native=True)
        request, metadata = ContextManager(agent).prepare_request(
            "continue",
            protocol="native",
            native_tail=_group("old", "feedback"),
            action_required=True,
        )
        assert len(request.messages) == 1
        assert metadata["selected_tool_call_ids"] == []
        assert metadata["dropped_tool_call_ids"] == ["old"]
        assert metadata["tool_tail_drops"] == [{"call_id": "old", "reason": "forced_action"}]


@pytest.mark.parametrize("native", [False, True])
def test_plan_request_lends_memory_budget_to_complete_fresh_source(tmp_path, native):
    from tests.test_code_relations import _observe, _setup

    root, state, context, service = _setup(tmp_path, "test_relation")
    (root / "large.py").write_text('def large():\n    return "' + "word " * 2000 + '"\n')
    try:
        oid = _observe(state, context, service, "large.py")
        service.relations({})
        with session_for(root) as session:
            session.create(simple_plan(session))
            session.configure_long_task(
                "inspect implementation", hard_constraints=["preserve tests"]
            )
            # XML includes textual tool signatures; allow the entire snippet
            # to fit after those protected rules, rather than expecting growth.
            limit = 4000 if native else 4500
            agent, _ = _agent(root, session, native=native, budget=limit)
            agent.tool_context = context
            context.exploration_service = service
            manager = ContextManager(agent)
            manager._get_workspace = lambda: ""
            manager._get_memory = lambda: "old memory " * 5000
            manager._get_knowledge = lambda query: ""
            manager._get_compressed_history = lambda metadata: ""
            request, metadata = manager.prepare_request(
                "continue", protocol="native" if native else "xml"
            )
            text = str(request.messages)
            assert "def large():" in text and "word word" in text
            assert "snippet_freshness=fresh" in text
            allocation = metadata["elastic_budget"]["allocations"]["source"]
            assert allocation["used_tokens"] > 1500
            assert allocation["borrowed_tokens"] > 0
            assert agent.session["context_manifest"]["source_observation_refs"] == [oid]
            assert "preserve tests" in text
            assert metadata["provider_input_tokens"] <= limit
            assert manager.budget.total_limit == limit
    finally:
        service.close()


def test_native_tail_recency_drops_are_explicit_in_final_manifest(tmp_path):
    (tmp_path / "value.py").write_text("value = 1\n")
    with session_for(tmp_path) as session:
        session.create(simple_plan(session))
        session.configure_long_task("task")
        agent, _ = _agent(tmp_path, session, native=True)
        tail = [
            message
            for name in ("old", "recent-1", "recent-2", "recent-3")
            for message in _group(name, "feedback")
        ]
        request, metadata = ContextManager(agent).prepare_request(
            "continue", protocol="native", native_tail=tail
        )
        assert len(native_groups(request.messages[1:])) == 3
        assert metadata["tool_tail_drops"] == [{"call_id": "old", "reason": "recency_limit"}]
        assert agent.session["context_manifest"]["tool_tail_drops"] == metadata["tool_tail_drops"]


def test_source_removed_by_protocol_budget_is_absent_from_final_selection(tmp_path):
    from agent_runtime.context_runtime import ContextItem, ContextSelectionResult

    (tmp_path / "value.py").write_text("value = 1\n")
    with session_for(tmp_path) as session:
        session.create(simple_plan(session))
        session.configure_long_task("task")
        agent, _ = _agent(tmp_path, session, native=True, budget=1800)
        manager = ContextManager(agent)
        manager._get_workspace = lambda: ""
        manager._get_memory = lambda: ""
        manager._get_knowledge = lambda query: ""
        manager._get_compressed_history = lambda metadata: ""

        def source(metadata, pool, *, elastic):
            body = manager.budget.fit('"' * 20000, pool - 50)
            item = ContextItem(
                "source:test",
                "source",
                body,
                source_ref="OBS-test",
                token_cost=manager.budget.count(body),
            )
            manager._source_selection = ContextSelectionResult(
                selected=[item], token_budget=pool, used_tokens=item.token_cost
            )
            return "## 当前代码片段\n" + body

        manager._get_source = source
        _, metadata = manager.prepare_request("continue", protocol="native")
        manifest = agent.session["context_manifest"]
        assert "elastic:source:protocol_budget" in metadata["cuts"]
        assert "source:test" not in manifest["selected_context_ids"]
        assert "source:test" in manifest["dropped_context_ids"]
        assert manifest["source_observation_refs"] == []
        assert manifest["selection"]["selected_ids"] == []
        assert manifest["selection"]["used_tokens"] == 0
