"""Admission precedes ranking, projection, direct-ID mutation and maintenance."""

import copy
from types import SimpleNamespace

import pytest

from agent_runtime.config import AgentConfig
from agent_runtime.features.memory.access import (
    MemoryAccessContext,
    agent_access_context,
    agent_governance,
    publish_project_memory,
)
from agent_runtime.features.memory.candidate import Candidate
from agent_runtime.features.memory.governance import MemoryGovernanceService
from agent_runtime.providers.clients import FakeModelClient
from agent_runtime.runtime import Agent
from agent_runtime.tool_context import ToolContext
from agent_runtime.workspace import WorkspaceContext


def candidate(value="pytest private fact", *, memory_id=""):
    return Candidate("project-conventions", "test.command", value, memory_id=memory_id)


def service(state, project="project-a", task="run-a", user="local", **kwargs):
    return MemoryGovernanceService(
        state, access_context=MemoryAccessContext(project, task, user), **kwargs,
    )


@pytest.mark.parametrize("scope", ["run", "task", "repository", "repository_version", "user"])
@pytest.mark.parametrize("other", ["project", "user"])
def test_foreign_owner_cannot_read_mutate_or_maintain(scope, other):
    state = {}
    owner = service(state, repo_fingerprint="version-a")
    memory = owner.ingest(candidate(), scope=scope)
    reader = service(
        state, project="project-b" if other == "project" else "project-a",
        user="bob" if other == "user" else "local", repo_fingerprint="version-a",
    )
    original = copy.deepcopy(state["governed_memories"])
    assert reader.recall("pytest") == []
    assert reader.inspect(memory.memory_id) is None
    assert reader.list_memories() == []
    assert not reader.confirm(memory.memory_id)
    assert not reader.reject(memory.memory_id)
    assert not reader.pin(memory.memory_id)
    assert not reader.forget(memory.memory_id)
    assert not reader.bind_evidence(memory.memory_id, ["spoofed"])
    assert not reader.validate(memory.memory_id, passed=True)
    assert not reader.record_usage(memory.memory_id, outcome="fixed")
    assert reader.record_usage_stage(memory.memory_id, usage="applied", stage="patch") == {}
    assert reader.record_recall([memory.memory_id]) == []
    reader.run(user_confirmed=True)
    assert state["governed_memories"] == original


@pytest.mark.parametrize("scope", ["run", "task"])
def test_run_and_missing_identity_cannot_widen_access(scope):
    state = {}
    owner = service(state)
    memory = owner.ingest(candidate(), scope=scope)
    for caller in (service(state, task="run-b"), service(state, task="")):
        assert caller.recall("pytest", task_id="run-a") == []
        assert caller.inspect(memory.memory_id) is None
        assert not caller.forget(memory.memory_id)
    assert owner.recall("pytest", task_id="") == []
    assert owner.recall("pytest")[0]["memory_id"] == memory.memory_id
    with pytest.raises(PermissionError):
        service({}, task="").ingest(candidate(), scope=scope)


def test_namespaced_ids_and_conflicts_do_not_merge_runs():
    state = {}
    first, second = service(state), service(state, task="run-b")
    one = first.ingest(candidate(memory_id="model-chosen"), scope="task")
    two = second.ingest(candidate("tox private fact", memory_id="model-chosen"), scope="task")
    assert one.memory_id != two.memory_id
    first.consolidate()
    second.consolidate()
    assert not state["memory_conflicts"]
    assert len(state["governed_memories"]) == 2


def test_legacy_and_wrong_version_are_withheld_before_recall():
    state = {}
    owner = service(state, repo_fingerprint="version-a")
    versioned = owner.ingest(candidate(), scope="repository_version", repo_fingerprint="old")
    ordinary = owner.ingest(candidate("pytest general rule"), scope="repository")
    raw = dict(state["governed_memories"][ordinary.memory_id])
    raw.pop("project_id")
    raw["memory_id"] = "legacy"
    state["governed_memories"]["legacy"] = raw
    ids = {item["memory_id"] for item in owner.recall("pytest")}
    assert ids == {ordinary.memory_id}
    assert owner.inspect(versioned.memory_id) is None
    assert owner.inspect("legacy") is None
    assert owner.confirm(versioned.memory_id)  # The owner can revalidate stale knowledge.


@pytest.fixture
def make_agent(tmp_path, monkeypatch):
    monkeypatch.setenv("FIXLOOP_STATE_ROOT", str(tmp_path / "shared"))

    def make(name, source):
        root = tmp_path / name
        root.mkdir()
        source.mkdir(exist_ok=True)
        agent = Agent(
            AgentConfig(provider="fake"), FakeModelClient(["<final>done</final>"]),
            WorkspaceContext(cwd=str(root), repo_root=str(root), doc_contents={}),
            tool_context=ToolContext(
                root=str(root), project_root=str(source), state_root=str(tmp_path / "runs" / name),
            ),
        )
        agent._memory_task_id = name
        agent._workspace_pinned = True
        return agent

    return make


def test_runtime_ignores_spoofed_session_identity(make_agent, tmp_path):
    agent = make_agent("run-a", tmp_path / "source")
    trusted = agent_access_context(agent)
    agent.session["memory"]["memory_identity"] = {
        "project_id": "other", "task_id": "other", "user_id": "admin",
    }
    assert agent_access_context(agent) == trusted


def test_confirmed_publication_shares_project_knowledge_but_not_run_candidates(
    make_agent, tmp_path,
):
    source = tmp_path / "source"
    first, second = make_agent("run-a", source), make_agent("run-b", source)
    other = make_agent("run-c", tmp_path / "other")
    local = agent_governance(first)
    memory = local.ingest(candidate(), scope="task")
    local.confirm(memory.memory_id)
    local.run(user_confirmed=True)
    assert agent_governance(second).recall("pytest") == []
    assert agent_governance(second, shared_read=True).recall("pytest") == []
    with pytest.raises(PermissionError):
        publish_project_memory(first, memory.memory_id)
    published_id = publish_project_memory(first, memory.memory_id, user_confirmed=True)
    shared = agent_governance(second, shared_read=True)
    published = shared.recall("pytest")[0]
    assert published["memory_id"] == published_id
    assert published["parent_memory_id"] == memory.memory_id
    assert published["source_run_id"] == "run-a"
    assert not shared.forget(published_id)
    assert not shared.confirm(published_id)
    assert not shared.record_usage(published_id, outcome="failed")
    with pytest.raises(PermissionError):
        shared.ingest(candidate())
    assert agent_governance(other, shared_read=True).recall("pytest") == []


def test_markdown_does_not_bypass_governance_into_context(make_agent, tmp_path):
    from agent_runtime.context_manager import ContextManager
    from agent_runtime.features.memory.durable import DurableMemoryStore

    agent = make_agent("run-a", tmp_path / "source")
    DurableMemoryStore(agent.state_root).promote([("project-conventions", "pytest legacy secret")])
    assert "legacy secret" not in ContextManager(agent)._get_knowledge("pytest")
    owned = agent_governance(agent).ingest(candidate("pytest permitted fact"), scope="task")
    knowledge = ContextManager(agent)._get_knowledge("pytest")
    assert "permitted fact" in knowledge
    assert owned.memory_id in agent.session["memory"]["recalled_memory_ids"]


def test_finalization_does_not_publish_project_memory(make_agent, tmp_path):
    from agent_runtime.loop_finalizer import _promote_memory_candidates

    source = tmp_path / "source"
    first, second = make_agent("run-a", source), make_agent("run-b", source)
    first.session["_memory_candidates"] = [candidate("pytest proposed rule")]
    _promote_memory_candidates(first, SimpleNamespace(final_answer="", user_request=""))
    assert agent_governance(first).list_memories()
    assert agent_governance(second, shared_read=True).recall("pytest") == []


def test_wrong_project_cannot_restore_session_even_with_shared_state(
    make_agent, tmp_path,
):
    from agent_runtime.session_store import SessionStore

    first = make_agent("run-a", tmp_path / "source")
    store = SessionStore(first._cwd, state_root=first.state_root)
    store.save(first.session)
    second = make_agent("run-b", tmp_path / "other")
    with pytest.raises(ValueError, match="workspace_mismatch"):
        Agent.from_session(
            FakeModelClient([]), second.workspace, store, first.session["id"],
            config=AgentConfig(provider="fake"), tool_context=second.tool_context,
        )


def test_versioned_publication_uses_content_not_copy_path_and_rejects_changes(
    make_agent, tmp_path,
):
    from pathlib import Path

    source = tmp_path / "source"
    first, second = make_agent("run-a", source), make_agent("run-b", source)
    for agent in (first, second):
        Path(agent._cwd, "code.py").write_text("value = 1", encoding="utf-8")
    local = agent_governance(first)
    memory = local.ingest(candidate(), scope="repository_version")
    publish_project_memory(first, memory.memory_id, user_confirmed=True)
    assert agent_governance(second, shared_read=True).recall("pytest")
    Path(second._cwd, "code.py").write_text("value = 2", encoding="utf-8")
    assert agent_governance(second, shared_read=True).recall("pytest") == []


def test_foreign_versions_are_filtered_without_reading_current_workspace():
    state = {}
    owner = service(state, repo_fingerprint="version-a")
    owner.ingest(candidate(), scope="repository_version")

    def forbidden():
        pytest.fail("foreign memory must not trigger a version read")

    reader = service(state, project="project-b", version_provider=forbidden)
    assert reader.recall("pytest") == []
    assert reader.list_memories() == []
    reader.run()


def test_precedents_enforce_owner_before_semantic_ranking(tmp_path):
    from src.repair.precedent import RepairPrecedentStore

    root, storage = tmp_path / "source", tmp_path / "run"
    root.mkdir()
    owner = MemoryAccessContext("project-a", "run-a")
    store = RepairPrecedentStore(str(root), state_root=str(storage), access_context=owner)
    store.upsert("type_error", "pytest private precedent")
    for context in (MemoryAccessContext("project-b", "run-a"), MemoryAccessContext("project-a", "")):
        other = RepairPrecedentStore(str(root), state_root=str(storage), access_context=context)
        assert other.load_all() == []
        assert other.load_similar("type_error", query="pytest") == []
        with pytest.raises(PermissionError):
            other.upsert("type_error", "forged")
    assert store.load_all()[0]["summary"] == "pytest private precedent"


def test_unwritable_memory_stays_local_and_publication_reports_failure(
    make_agent, tmp_path, monkeypatch,
):
    from pathlib import Path

    agent = make_agent("run-a", tmp_path / "source")

    def denied(*args, **kwargs):
        raise PermissionError("storage unavailable")

    monkeypatch.setattr(Path, "mkdir", denied)
    local = agent_governance(agent)
    memory = local.ingest(candidate(), scope="task")
    assert local.recall("pytest")
    assert agent.session["memory"]["memory_persistence"] == "memory_only"
    with pytest.raises(OSError, match="storage unavailable"):
        publish_project_memory(agent, memory.memory_id, user_confirmed=True)


def test_same_session_resume_keeps_owned_memory_and_new_session_cannot_read_it(
    make_agent, tmp_path,
):
    from agent_runtime.session_store import SessionStore

    first = make_agent("run-a", tmp_path / "source")
    first._memory_task_id = ""  # Ordinary conversation uses its immutable session boundary.
    owned = agent_governance(first).ingest(candidate(), scope="task")
    store = SessionStore(first._cwd, state_root=first.state_root)
    store.save(first.session)
    restored = Agent.from_session(
        FakeModelClient([]), first.workspace, store, first.session["id"],
        config=AgentConfig(provider="fake"), tool_context=first.tool_context,
    )
    assert agent_governance(restored).inspect(owned.memory_id)
    fresh = Agent(
        AgentConfig(provider="fake"), FakeModelClient([]), first.workspace,
        tool_context=first.tool_context,
    )
    assert agent_governance(fresh).recall("pytest") == []


def test_sandbox_resume_uses_binding_identity_for_memory(temp_workspace, tmp_path):
    from agent_runtime.linux_sandbox.task_context import SandboxTaskBinding, SandboxTaskSpec
    from tests.test_repair_factory import _prepared_environment
    from tests.test_sandbox_task_context import metadata_backend

    copy_root = tmp_path / "copy"
    copy_root.mkdir()
    spec = SandboxTaskSpec(
        _prepared_environment().identity, "memory-v1", "a" * 64,
        str(tmp_path / "storage"), str(temp_workspace),
    )
    binding = SandboxTaskBinding.create(copy_root, copy_root, "task-a", spec)
    binding.save()
    memory_id = ""
    for current in (binding, SandboxTaskBinding.load(copy_root, "task-a", spec)):
        agent = Agent(
            AgentConfig(provider="fake"), FakeModelClient([]),
            WorkspaceContext(cwd=str(copy_root), repo_root=str(copy_root), doc_contents={}),
            tool_context=ToolContext(
                root=str(copy_root), sandbox_mode=True,
                embedding_state_base=str(tmp_path / "storage"),
            ),
        )
        agent.bind_sandbox_task(current, metadata_backend(current))
        governance = agent_governance(agent)
        assert governance.task_id == "task-a"
        if not memory_id:
            memory_id = governance.ingest(candidate(), scope="task").memory_id
        else:
            assert governance.inspect(memory_id)
