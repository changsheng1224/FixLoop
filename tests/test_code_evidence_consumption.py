"""Code evidence is checked at consumption, independently of query coverage."""

from __future__ import annotations

import hashlib
import json

import pytest

from agent_runtime.cancellation import CancellationToken
from agent_runtime.code_exploration.consumption import SourceChecks
from agent_runtime.code_exploration.io import read_file_result
from agent_runtime.code_exploration.models import RetrievalLimits
from agent_runtime.config import AgentConfig
from agent_runtime.context_manager import ContextManager
from agent_runtime.context_runtime import ObservationStore
from agent_runtime.providers.clients import FakeModelClient, FakeNativeToolClient
from agent_runtime.runtime import Agent
from agent_runtime.tool_context import ToolContext
from agent_runtime.tools import tool_expand_observation
from agent_runtime.workspace import WorkspaceContext


def _record(root, *, state=None, state_root="", end=None):
    state = state if state is not None else {"id": "session", "run_id": "run"}
    context = ToolContext(root=str(root), observation_state=state, state_root=state_root)
    args = {"path": "mod.py", **({"end": end} if end is not None else {})}
    result = read_file_result(context, args)
    store = ObservationStore(state, str(root), state_root)
    record = store.put(
        "read_file",
        args,
        result.content,
        source_dependencies=result.metadata["source_dependencies"],
        retrieval_query_id=result.metadata["retrieval_result"]["query_id"],
        retrieval_result=result.metadata["retrieval_result"],
    )
    return state, context, store, record


def test_contract_survives_disk_reload_and_expansion_truncation(tmp_path):
    (tmp_path / "mod.py").write_text("value = 42\n" * 20, encoding="utf-8")
    state, context, store, record = _record(tmp_path)
    contract = record.retrieval_result
    store.close()
    restored = ObservationStore({"id": "session", "run_id": "run"}, str(tmp_path))
    try:
        assert restored.get(record.observation_id).retrieval_result == contract
        expanded = restored.expand_for_context(record.observation_id, max_tokens=1)
        assert expanded["ok"] and expanded["freshness"] == "fresh"
        assert expanded["output_truncated"]
        assert expanded["retrieval_result"]["completeness"] == "complete_in_scope"
        assert (
            "output_truncated=true"
            in tool_expand_observation(
                context, {"observation_id": record.observation_id, "max_tokens": 1}
            ).content
        )
    finally:
        restored.close()


@pytest.mark.parametrize(
    "mutation,reason",
    [
        ("edit", "source_changed"),
        ("delete", "source_unavailable"),
        ("blob", "blob_unavailable"),
        ("run", "scope_mismatch"),
    ],
)
def test_changed_or_corrupt_evidence_is_rejected_but_archive_is_preserved(
    tmp_path, mutation, reason
):
    source = tmp_path / "mod.py"
    source.write_text("value = 'original'\n", encoding="utf-8")
    state, context, store, record = _record(tmp_path)
    try:
        if mutation == "edit":
            source.write_text("value = 'changed'\n", encoding="utf-8")
        elif mutation == "delete":
            source.unlink()
        elif mutation == "blob":
            from pathlib import Path

            Path(record.raw_ref).write_text("corrupt", encoding="utf-8")
        else:
            store.run_id = "new-run"
        result = store.expand_for_context(record.observation_id, context=context)
        assert not result["ok"] and result["reason"] == reason
        assert "content" not in result
        if mutation in {"edit", "delete"}:
            assert "original" in store.expand(record.observation_id)
        assert store.get(record.observation_id).stale
    finally:
        store.close()


def test_partial_coverage_and_freshness_are_independent(tmp_path):
    (tmp_path / "mod.py").write_text("value = 1\n", encoding="utf-8")
    _, context, store, record = _record(tmp_path)
    try:
        store.registry[record.observation_id]["retrieval_result"].update(
            completeness="partial",
            truncation_reasons=["hit_limit"],
            scanned_scope={"root": ".", "glob": "*.py"},
            degradation_reason="lsp_unavailable",
        )
        content = tool_expand_observation(
            context, {"observation_id": record.observation_id}
        ).content
        assert "freshness=fresh" in content and "completeness=partial" in content
        assert "hit_limit" in content and "lsp_unavailable" in content
        assert "*.py" in content and "absence applies only" in content
    finally:
        store.close()


def test_unversioned_range_is_unknown_and_requires_explicit_reread(tmp_path):
    (tmp_path / "mod.py").write_text("original = 1\nmore = 2\n", encoding="utf-8")
    _, context, store, record = _record(tmp_path, end=1)
    try:
        assert not record.retrieval_result["dependency_versions"]
        result = store.expand_for_context(record.observation_id, context=context)
        assert result["freshness"] == "unknown" and result["reason"] == "unversioned_source"
        assert not store.get(record.observation_id).stale
        content = tool_expand_observation(
            context, {"observation_id": record.observation_id}
        ).content
        assert "freshness=unknown" in content and "original = 1" not in content
    finally:
        store.close()


@pytest.mark.parametrize("stop", ["bytes", "files", "cancel", "deadline"])
def test_checks_obey_host_budgets_and_do_not_mark_unknown_as_stale(tmp_path, stop):
    (tmp_path / "mod.py").write_text("value = 1\n", encoding="utf-8")
    _, context, store, record = _record(tmp_path)
    try:
        context.exploration_limits = (
            RetrievalLimits(search_read_bytes=1) if stop == "bytes" else RetrievalLimits()
        )
        if stop == "files":
            context.exploration_limits = RetrievalLimits(search_files=0)
        if stop == "cancel":
            context.cancel_token = CancellationToken()
            context.cancel_token.cancel()
        if stop == "deadline":
            from types import SimpleNamespace

            context.deadline = SimpleNamespace(remaining_s=lambda: 0)
        checks = SourceChecks(context)
        result = store.expand_for_context(record.observation_id, source_checks=checks)
        assert not result["ok"] and result["freshness"] == "unknown"
        assert not store.get(record.observation_id).stale
        assert checks.bytes_read <= context.exploration_limits.search_read_bytes
    finally:
        store.close()


def test_state_root_is_not_used_as_source_root_and_hashes_are_shared(tmp_path):
    root = tmp_path / "workspace"
    root.mkdir()
    (root / "mod.py").write_text("value = 1\n", encoding="utf-8")
    _, context, store, record = _record(root, state_root=str(tmp_path / "state"))
    try:
        checks = SourceChecks(context)
        for _ in range(2):
            assert store.expand_for_context(record.observation_id, source_checks=checks)["ok"]
        assert checks.checked_files == 1 and checks.bytes_read == (root / "mod.py").stat().st_size
        versions = {"../external.py": hashlib.sha256(b"secret").hexdigest()}
        assert checks.check(versions, root.resolve()) == ("stale", "source_policy_changed")
    finally:
        store.close()


@pytest.mark.parametrize("native", [False, True])
def test_actual_model_request_drops_changed_source_in_history_and_native_tail(tmp_path, native):
    source = tmp_path / "mod.py"
    source.write_text("old_marker = 1\n", encoding="utf-8")
    base = FakeNativeToolClient if native else FakeModelClient

    class RecordingClient(base):
        def __init__(self):
            super().__init__(
                [
                    '<tool>{"name":"read_file","args":{"path":"mod.py"}}</tool>',
                    '<tool>{"name":"list_files","args":{"path":"."}}</tool>',
                    "<final>done</final>",
                ]
            )
            self.inputs = []

    if native:

        def complete_turn(self, request):
            self.inputs.append(json.dumps(request.messages, ensure_ascii=False))
            if len(self.inputs) == 2:
                source.write_text("new_marker = 2\n", encoding="utf-8")
            return base.complete_turn(self, request)

        RecordingClient.complete_turn = complete_turn

    else:

        def complete(self, prompt, *args, **kwargs):
            self.inputs.append(prompt)
            if len(self.inputs) == 2:
                source.write_text("new_marker = 2\n", encoding="utf-8")
            return base.complete(self, prompt, *args, **kwargs)

        RecordingClient.complete = complete

    client = RecordingClient()
    agent = Agent(
        config=AgentConfig(provider="fake", max_steps=5),
        model_client=client,
        workspace=WorkspaceContext.build(str(tmp_path)),
        cwd=str(tmp_path),
    )
    assert agent.ask("inspect the module", skip_plan=True) == "done"
    assert "old_marker = 1" in client.inputs[1]
    assert "old_marker = 1" not in client.inputs[2]
    assert "new_marker = 2" not in client.inputs[2]
    assert "source_changed" in client.inputs[2]
    assert "old_marker = 1" in json.dumps(agent.read_history())
    if native:
        assert "tool_use_id" in client.inputs[2]


def test_empty_partial_query_retains_boundaries_in_actual_context(tmp_path):
    agent = Agent(
        config=AgentConfig(provider="fake"),
        model_client=FakeModelClient([]),
        workspace=WorkspaceContext.build(str(tmp_path)),
        cwd=str(tmp_path),
    )
    store = ObservationStore(agent.session, str(tmp_path))
    try:
        result = {
            "query_type": "grep",
            "execution": "ok",
            "completeness": "partial",
            "scanned_scope": {"root": ".", "glob": "*.py", "files_scanned": 1},
            "hits": [],
            "truncation_reasons": ["read_bytes"],
            "dependency_versions": {},
        }
        record = store.put(
            "grep",
            {"pattern": "absent"},
            "0 matches",
            source_dependencies={},
            retrieval_result=result,
        )
        agent.session["history"] = [
            {"role": "tool", "observation_id": record.observation_id, "content": "0 matches"}
        ]
        text, metadata = ContextManager(agent).build_dynamic_context("inspect")
        assert "completeness=partial" in text and "freshness=unknown" in text
        assert "read_bytes" in text and "*.py" in text
        assert metadata["code_evidence"][record.observation_id]["reason"] == "unversioned_source"
    finally:
        store.close()


def test_hashes_of_some_hits_do_not_make_unversioned_hits_fresh(tmp_path):
    (tmp_path / "mod.py").write_text("value = 1\n", encoding="utf-8")
    _, context, store, record = _record(tmp_path)
    try:
        store.registry[record.observation_id]["retrieval_result"]["hits"].append(
            {"path": "unhashed.py", "source": "text", "resolution": "candidate"}
        )
        result = store.expand_for_context(record.observation_id, context=context)
        assert not result["ok"] and result["freshness"] == "unknown"
        assert result["reason"] == "unversioned_source"
    finally:
        store.close()


@pytest.mark.parametrize("native", [False, True])
def test_change_immediately_after_tool_does_not_leak_through_continuation(tmp_path, native):
    source = tmp_path / "mod.py"
    source.write_text("old_marker = 1\n", encoding="utf-8")
    base = FakeNativeToolClient if native else FakeModelClient
    client = base(
        [
            '<tool>{"name":"read_file","args":{"path":"mod.py"}}</tool>',
            "<final>done</final>",
        ]
    )
    requests = []
    if native:
        original_turn = client.complete_turn

        def capture(request):
            requests.append(json.dumps(request.messages))
            return original_turn(request)

        client.complete_turn = capture
    agent = Agent(
        config=AgentConfig(provider="fake", max_steps=3),
        model_client=client,
        workspace=WorkspaceContext.build(str(tmp_path)),
        cwd=str(tmp_path),
    )
    original_record = agent.record

    def record(item):
        original_record(item)
        if item.get("role") == "tool":
            source.write_text("new_marker = 2\n", encoding="utf-8")

    agent.record = record
    assert agent.ask("inspect", skip_plan=True) == "done"
    later = requests[1] if native else client.prompts[1]
    assert "old_marker = 1" not in later and "new_marker = 2" not in later
    assert "source_changed" in later


def test_l2_real_read_patch_and_host_verifier_keep_contracts(tmp_path):
    from tests.plan_l2_support import repair_fixture

    orch, state, client = repair_fixture(tmp_path)
    try:
        patches, meta = orch._run_patcher_toolized(state, "repair", {})
        assert patches, (meta, state.agent_errors)
        assert orch._run_verifier(state).all_passed
        assert "return 2" in (tmp_path / "value.py").read_text()
        assert any("completeness=complete_in_scope" in prompt for prompt in client.prompts)
        assert any("freshness=fresh" in prompt for prompt in client.prompts)
        journal = list(
            orch._plan_binding.session.store.latest("operation", "operation_id").values()
        )
        assert (
            sum(
                item["tool"] == "patch_file"
                and item["phase"] == "result_recorded"
                and item["status"] == "success"
                for item in journal
            )
            == 1
        )
    finally:
        if orch._plan_binding:
            orch._plan_binding.close()


def test_task_scope_uses_runtime_identity_and_rejects_cross_task_expansion(tmp_path):
    (tmp_path / "mod.py").write_text("old_marker = 1\n", encoding="utf-8")
    state = {"id": "session", "session_identity": {"task_id": "task", "run_id": "run"}}
    _, context, store, record = _record(tmp_path, state=state)
    store.close()
    assert record.run_id == "run" and record.task_id == "task"
    state["session_identity"]["task_id"] = "other-task"
    reopened = ObservationStore(state, str(tmp_path))
    try:
        assert (
            reopened.expand_for_context(record.observation_id, context=context)["reason"]
            == "scope_mismatch"
        )
    finally:
        reopened.close()


def test_restored_checkpoint_with_changed_source_cannot_replay_old_tool_body(tmp_path):
    from agent_runtime.agent_loop import AgentLoop
    from agent_runtime.checkpoint import evaluate_resume_state
    from agent_runtime.session_store import SessionStore
    from agent_runtime.task_state import TaskState
    from tests.test_code_exploration_resume import _agent

    source = tmp_path / "service.py"
    source.write_text("def obsolete_marker():\n    return 1\n", encoding="utf-8")
    first = _agent(tmp_path, [])
    loop = AgentLoop(first)
    task = TaskState.create(user_request="inspect")
    task.advance_runtime("reasoning")
    loop._task_state = task
    loop._run_tool_step(task, "read_file", {"path": "service.py"}, step=1, path="xml")
    client = FakeModelClient(["<final>resumed</final>"])
    restored = Agent.from_session(
        client,
        WorkspaceContext.build(str(tmp_path)),
        SessionStore(str(tmp_path)),
        first.session["id"],
        config=AgentConfig(provider="fake", max_steps=5, code_exploration={"mode": "relations"}),
        cwd=str(tmp_path),
    )
    restored.session["resume_state"] = evaluate_resume_state(restored)
    assert restored.session["resume_state"]["status"] == "step-resumable"
    # Mutation after recovery assessment must still be caught at consumption.
    source.write_text("def current_marker():\n    return 2\n", encoding="utf-8")
    assert "resumed" in restored.ask("continue")
    assert "obsolete_marker" not in client.prompts[0]
    assert "current_marker" not in client.prompts[0]
    assert "source_changed" in client.prompts[0]
    first.tool_context.exploration_service.close()


def test_selected_snippet_freshness_does_not_promote_parent_query(tmp_path):
    from agent_runtime.code_exploration.context import select_source_context
    from agent_runtime.context_manager import TokenBudget
    from tests.test_code_relations import _observe, _setup

    _, state, context, service = _setup(tmp_path, "test_relation")
    try:
        oid = _observe(state, context, service, "service.py")
        service.evidence[oid].retrieval_result["hits"].append(
            {"path": "unversioned.py", "source": "text", "resolution": "candidate"}
        )
        service.relations({})
        text, selection = select_source_context(
            service,
            TokenBudget(),
            role="",
            phase="repair",
            token_limit=1500,
        )
        assert selection.selected
        assert "snippet_freshness=fresh" in text
        assert "completeness=complete_in_scope freshness=unknown" in text
        assert "text:candidate" in text
    finally:
        service.close()
