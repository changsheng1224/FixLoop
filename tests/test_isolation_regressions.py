"""S1–S4 composition: real entries/stores/prompts with deterministic execution."""

import copy
import hashlib
import json
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest

from agent_runtime.config import AgentConfig
from agent_runtime.features.memory import semantic
from agent_runtime.features.memory.access import agent_governance, publish_project_memory
from agent_runtime.features.memory.candidate import Candidate
from agent_runtime.features.memory.store import CanonicalMemoryStore
from agent_runtime.providers.clients import FakeModelClient
from agent_runtime.runtime import Agent
from agent_runtime.session_store import SessionStore
from agent_runtime.state_root import project_id, workspace_id
from agent_runtime.tool_context import ToolContext
from agent_runtime.worktree import _git
from src.repair.assembly import create_repair_service
from src.repair.checkpoint_load import save_repair_checkpoint
from src.repair.service import RepairRequest, RepairService
from src.state import RepairState
from src.workbench.project import ProjectExecutor
from src.workbench.store import WorkbenchStore
from tests.workbench_unit_service import WorkbenchService


@pytest.fixture
def encodings(tmp_path, monkeypatch):
    """Exercise real cache I/O without loading or downloading model weights."""
    calls = []
    monkeypatch.setenv("FIXLOOP_STATE_ROOT", str(tmp_path / "storage"))

    def load(config):

        def encode(text, **kwargs):
            calls.append(text)
            seed = int(hashlib.sha256(text.encode()).hexdigest()[:8], 16)
            return np.random.default_rng(seed).random(config.dimensions, dtype=np.float32)

        return SimpleNamespace(encode=encode)

    monkeypatch.setattr(semantic, "_get_semantic_model", load)
    return calls


def orchestrator(root, storage, *, tier="host"):
    return create_repair_service(
        FakeModelClient(["<final>done</final>"]),
        str(root),
        skip_verify=True,
        state_root=str(storage),
        workspace_policy="worktree",
        execution_tier=tier,
        runtime_overrides={"provider": "fake", "model": "fake", "approval": "auto"},
    ).session


def fact(value="pytest PRIVATE_RUN_FACT"):
    return Candidate("project-conventions", "test.command", value, memory_id="model-same-id")


@pytest.mark.parametrize("separate_project", [False, True], ids=["same-project", "clones"])
def test_concurrent_entries_partition_code_state_memory_and_cache(
    temp_workspace, tmp_path, monkeypatch, encodings, separate_project
):
    roots = [temp_workspace, temp_workspace]
    if separate_project:
        roots[1] = tmp_path / "clone"
        _git(temp_workspace, "clone", str(temp_workspace), str(roots[1]))
    storage = tmp_path / "storage"
    orchs = [orchestrator(root, storage) for root in roots]
    run_ids = ["same-run", "same-run" if separate_project else "other-run"]
    barrier = threading.Barrier(2)
    resources = {}
    original = (temp_workspace / "README.md").read_bytes()
    for index, orch in enumerate(orchs):

        def body(state, initial_snapshot, *, orch=orch, index=index):
            agent = orch.patcher
            root = Path(agent._cwd)
            assert root != roots[index] and "README.md" in initial_snapshot
            governance = agent_governance(agent)
            local = governance.ingest(fact(f"pytest PRIVATE_{index}"), scope="task")
            memory = agent.semantic_memory
            vector = memory.embed("identical encoded input")
            barrier.wait(timeout=20)
            (root / "README.md").write_text(f"candidate-{index}", encoding="utf-8")
            save_repair_checkpoint(state, str(root), state_root=agent.state_root)
            resources[index] = (agent, local.memory_id, memory, vector)
            return state

        monkeypatch.setattr(orch.coordinator, "repair_impl", body)
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [
            pool.submit(orch.lifecycle.execute, "fix", run_id=run_id, repair_timeout_s=0)
            for orch, run_id in zip(orchs, run_ids, strict=True)
        ]
        states = [future.result(timeout=40) for future in futures]
    first, second = (resources[i] for i in range(2))
    a, b = (first[0], second[0])
    assert workspace_id(a._cwd) != workspace_id(b._cwd)
    assert a.state_root != b.state_root and first[1] != second[1]
    assert (a.memory_access.project_id == b.memory_access.project_id) is (not separate_project)
    assert (first[2].cache.root == second[2].cache.root) is (not separate_project)
    assert np.array_equal(first[3], second[3])
    for index, (agent, memory_id, memory, _) in resources.items():
        assert agent.memory_access.task_id == run_ids[index]
        assert agent.memory_access.project_id == project_id(roots[index])
        assert memory.cache.load("identical encoded input") is not None
        assert agent_governance(agent).inspect(memory_id)
        assert agent_governance(agent).inspect(resources[1 - index][1]) is None
        assert agent_governance(agent, shared_read=True).recall("pytest") == []
        assert states[index].node_timings["run_entry"]["workspace"]["root"] == agent._cwd
        assert (Path(agent._cwd) / "README.md").read_text() == f"candidate-{index}"
        assert (roots[index] / "README.md").read_bytes() == original
        assert not (roots[index] / ".agent").exists()
        agent.close()


@pytest.mark.parametrize("drift", [False, True], ids=["resume", "policy-drift"])
def test_retained_checkout_resume_keeps_run_memory_but_rechecks_content_and_policy(
    temp_workspace, tmp_path, monkeypatch, encodings, drift
):
    storage = tmp_path / "storage"
    first = orchestrator(temp_workspace, storage)
    state = RepairState(issue_input="fix", repair_run_id="retained")
    first.lifecycle.prepare_host_worktree(state, resume=False)
    agent = first.patcher
    local = agent_governance(agent)
    owned = local.ingest(fact(), scope="task")
    versioned = local.ingest(fact("pytest OLD_CONTENT_FACT"), scope="repository_version")
    memory = agent.semantic_memory
    original = (Path(agent._cwd) / "README.md").read_text()
    vector = memory.embed(original)
    old_key = memory.cache.path(original)
    state.execution.run_entry = first.lifecycle.workspace_entry()
    save_repair_checkpoint(state, agent._cwd, state_root=agent.state_root)
    (Path(agent._cwd) / "README.md").write_text("dirty candidate", encoding="utf-8")
    before = copy.deepcopy(local.store.list_memories())
    second = orchestrator(temp_workspace, storage, tier="static" if drift else "host")

    def body(restored, initial_snapshot):
        assert not drift, "policy drift reached execution"
        assert second.patcher._cwd == agent._cwd
        current = agent_governance(second.patcher)
        assert current.inspect(owned.memory_id)
        assert current.inspect(versioned.memory_id) is None
        assert "PRIVATE_RUN_FACT" in current.recall("pytest")[0]["value"]
        cache = second.patcher.semantic_memory
        assert cache.cache.root == memory.cache.root
        assert np.array_equal(cache.embed(original), vector)
        assert encodings == [original]
        cache.embed("dirty candidate")
        assert cache.cache.path("dirty candidate") != old_key
        assert encodings == [original, "dirty candidate"]
        return restored

    monkeypatch.setattr(second.coordinator, "repair_impl", body)
    if drift:
        monkeypatch.setattr(
            second.transaction,
            "snapshot_repo",
            lambda: pytest.fail("snapshot before rejection"),
        )
    result = RepairService(second).run(RepairRequest("fix", resume_run_id="retained", timeout_s=0))
    assert (result.status == "recovery_required") is drift
    if drift:
        assert result.agent_errors["checkpoint"] == "resume_workspace_policy_mismatch"
        assert encodings == [original]
    assert local.store.list_memories() == before
    assert (Path(agent._cwd) / "README.md").read_text() == "dirty candidate"
    assert (temp_workspace / "README.md").read_text() == original
    first.patcher.close()
    second.patcher.close()


@pytest.mark.parametrize("damage", ["project", "run", "missing-owner"])
def test_misrouted_persisted_record_never_enters_model_prompt_or_feedback(
    temp_workspace, tmp_path, encodings, damage
):
    orch = orchestrator(temp_workspace, tmp_path / "storage")
    orch.lifecycle.prepare_host_worktree(
        RepairState(issue_input="fix", repair_run_id="current"), resume=False
    )
    agent = orch.patcher
    local = agent_governance(agent)
    raw = local.ingest(fact(), scope="task").to_dict()
    if damage == "project":
        raw["project_id"] = "project-foreign"
    elif damage == "run":
        raw["task_id"] = "foreign-run"
    else:
        raw.pop("project_id")
    local.store.upsert_memory(raw)
    persisted = local.store.list_memories()
    agent.session["memory"].pop("governed_memories")
    assert agent.ask("pytest guidance", skip_plan=True) == "done"
    assert "PRIVATE_RUN_FACT" not in agent.model_client.prompts[-1]
    governance = agent_governance(agent)
    assert governance.inspect(raw["memory_id"]) is None
    assert not governance.record_usage(raw["memory_id"], outcome="fixed")
    assert CanonicalMemoryStore(agent.state_root).list_memories() == persisted
    agent.close()


def test_session_restore_cannot_relabel_source_project_in_same_execution_directory(
    temp_workspace, tmp_path, encodings
):
    orch = orchestrator(temp_workspace, tmp_path / "storage")
    orch.lifecycle.prepare_host_worktree(
        RepairState(issue_input="fix", repair_run_id="session"), resume=False
    )
    agent = orch.patcher
    owned = agent_governance(agent).ingest(fact(), scope="task")
    store = SessionStore(agent._cwd, state_root=agent.state_root)
    store.save(agent.session)
    other = tmp_path / "other-project"
    other.mkdir()
    with pytest.raises(ValueError, match="memory_session_project_mismatch"):
        Agent.from_session(
            FakeModelClient([]),
            agent.workspace,
            store,
            agent.session["id"],
            config=AgentConfig(provider="fake"),
            tool_context=ToolContext(
                root=agent._cwd, project_root=str(other), state_root=agent.state_root
            ),
        )
    assert agent_governance(agent).inspect(owned.memory_id)
    agent.close()


def test_workbench_fork_shares_only_published_knowledge_and_ancestor_history(
    tmp_path, monkeypatch, encodings
):
    source = tmp_path / "source"
    source.mkdir()
    (source / "value.py").write_text("value = 1\n", encoding="utf-8")
    clients, agents, requests, owned_ids = ([], [], [], [])
    shared_before = []
    original_ask = Agent.ask

    def client():
        model = FakeModelClient([f"<final>ANSWER_{len(clients)}</final>"])
        clients.append(model)
        return model

    def ask(agent, request, *args, **kwargs):
        agents.append(agent)
        requests.append(json.loads(request.split("\n", 1)[1]))
        governance = agent_governance(agent)
        assert governance.recall("pytest") == []
        owned = governance.ingest(fact(f"pytest LOCAL_{len(agents)}"), scope="task")
        owned_ids.append(owned.memory_id)
        agent.semantic_memory.embed("common derived input")
        if len(agents) == 1:
            approved = governance.ingest(fact("pytest APPROVED_PROJECT_FACT"), scope="repository")
            publish_project_memory(agent, approved.memory_id, user_confirmed=True)
            shared_before.extend(
                copy.deepcopy(agent_governance(agent, shared_read=True).store.list_memories())
            )
        return original_ask(agent, request, *args, **kwargs)

    monkeypatch.setattr(Agent, "ask", ask)
    executor = ProjectExecutor(client, provider="fake")
    with WorkbenchService(
        tmp_path / "workbench", project_executor=executor, project_lock_dir=tmp_path / "locks"
    ) as service:
        chat = service.create_conversation(project_path=str(source))
        first = service.send(chat.id, "pytest ORIGINAL_REQUEST", mode="project_chat")
        assert service.wait(chat.id, first.run.id, timeout=20).status == "succeeded"
        future = service.send(chat.id, "pytest FUTURE_REQUEST", mode="project_chat")
        assert service.wait(chat.id, future.run.id, timeout=20).status == "succeeded"
        original_tree = service.store.get_tree(chat.id)
        plan = service.preview_restore(chat.id, action="input", node_id=first.node.id)
        branch = service.edit(chat.id, first.node.id, "pytest EDITED_REQUEST", plan_id=plan["id"])
        assert service.wait(chat.id, branch.run.id, timeout=20).status == "succeeded"
        assert requests[1][-3:] == [
            {"role": "user", "content": "pytest ORIGINAL_REQUEST"},
            {"role": "assistant", "content": "ANSWER_0"},
            {"role": "user", "content": "pytest FUTURE_REQUEST"},
        ]
        assert requests[2] == [{"role": "user", "content": "pytest EDITED_REQUEST"}]
        assert {agent.memory_access.task_id for agent in agents} == {
            first.run.id,
            future.run.id,
            branch.run.id,
        }
        assert len({agent.state_root for agent in agents}) == 3
        assert len({workspace_id(agent._cwd) for agent in agents}) == 3
        assert len({agent.semantic_memory.cache.root for agent in agents}) == 1
        for index, model in enumerate(clients):
            prompt = model.prompts[-1]
            assert "APPROVED_PROJECT_FACT" in prompt and f"LOCAL_{index + 1}" in prompt
            assert all(f"LOCAL_{n}" not in prompt for n in (1, 2, 3) if n != index + 1)
            assert owned_ids[index] in agents[index].session["memory"]["recalled_memory_ids"]
        assert "FUTURE_REQUEST" not in clients[2].prompts[-1]
        assert "ANSWER_0" not in clients[2].prompts[-1]
        shared_after = agent_governance(agents[-1], shared_read=True).store.list_memories()
        assert shared_after == shared_before
        assert (source / "value.py").read_text() == "value = 1\n"
        assert not (source / ".agent").exists()
        database = service.store.database
    reopened = WorkbenchStore(database)
    tree = reopened.get_tree(chat.id)
    assert len(tree.branches) == 2
    assert all(run in tree.runs for run in original_tree.runs)
    assert reopened.get_run(chat.id, branch.run.id).status == "succeeded"
