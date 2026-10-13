"""Project scope, config invalidation, corruption and atomic publication without downloads."""

import hashlib
import os
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest

from agent_runtime.features.memory import semantic
from agent_runtime.features.memory.embedding_cache import (
    EmbeddingCache,
    EmbeddingConfig,
    project_embedding_cache_root,
)
from agent_runtime.state_root import native_io_path, project_id, trusted_state_root


class Encoder:
    def __init__(self, config):
        self.config = config
        self.calls = []

    def encode(self, text, **kwargs):
        self.calls.append((text, kwargs))
        seed = int(hashlib.sha256(text.encode()).hexdigest()[:8], 16)
        return np.random.default_rng(seed).random(self.config.dimensions, dtype=np.float32)


@pytest.fixture
def encoder(monkeypatch):
    models = []

    def load(config):
        model = Encoder(config)
        models.append(model)
        return model

    monkeypatch.setattr(semantic, "_get_semantic_model", load)
    return models


def test_project_partition_and_cwd_independence(tmp_path, monkeypatch, encoder):
    source_a, source_b, storage = (tmp_path / name for name in ("a", "b", "state"))
    source_a.mkdir()
    source_b.mkdir()
    roots = [project_embedding_cache_root(source, storage) for source in (source_a, source_b)]
    first = semantic.SemanticMemory(cache_root=roots[0])
    expected = first.embed("private project text")
    monkeypatch.chdir(source_b)
    same_project = semantic.SemanticMemory(cache_root=roots[0])
    assert np.array_equal(same_project.embed("private project text"), expected)
    assert not encoder[1].calls
    other_project = semantic.SemanticMemory(cache_root=roots[1])
    other_project.embed("private project text")
    assert len(encoder[2].calls) == 1
    assert roots[0] != roots[1]
    assert project_id(source_a) in roots[0].parts
    assert not (source_a / ".agent").exists() and (not (source_b / ".agent").exists())


@pytest.mark.parametrize(
    "changes",
    [
        {"model_id": "another/model"},
        {"revision": "b" * 40},
        {"dimensions": 16},
        {"normalize_embeddings": True},
        {"max_seq_length": 128},
        {"max_chars": 400},
        {"encoder_version": "new-library"},
        {"text_version": "chunk-v2"},
    ],
)
def test_encoding_changes_miss_instead_of_reusing_old_vector(tmp_path, encoder, changes):
    original = EmbeddingConfig()
    first = semantic.SemanticMemory(cache_root=tmp_path, config=original)
    first.embed("same text")
    second = semantic.SemanticMemory(cache_root=tmp_path, config=replace(original, **changes))
    second.embed("same text")
    assert len(encoder[1].calls) == 1
    assert first.cache.path("same text") != second.cache.path("same text")


def test_actual_encoded_text_and_chunk_query_share_encoder(tmp_path, encoder):
    memory = semantic.SemanticMemory(cache_root=tmp_path)
    memory.add({"text": "  repeated text  ", "note_index": 1})
    memory.add({"text": "repeated text", "note_index": 2})
    assert [call[0] for call in encoder[0].calls] == ["repeated text"]
    long_text = "chunk content " * 200
    memory.add({"text": long_text, "note_index": 3})
    calls_before = len(encoder[0].calls)
    fresh = memory.empty_copy()
    fresh.add({"text": long_text, "note_index": 4})
    assert len(encoder[0].calls) == calls_before
    memory.search("repeated text")
    memory.search("repeated text")
    assert len(encoder[0].calls) == calls_before
    assert all(
        (
            kwargs
            == {"convert_to_numpy": True, "precision": "float32", "normalize_embeddings": False}
            for _, kwargs in encoder[0].calls
        )
    )


@pytest.mark.parametrize(
    "damage", ["truncated", "archive", "object", "dimension", "nan", "zero", "dtype"]
)
def test_corruption_is_recomputed_and_replaced(tmp_path, encoder, damage):
    memory = semantic.SemanticMemory(cache_root=tmp_path)
    expected = memory.embed("recoverable")
    target = memory.cache.path("recoverable")
    if damage == "truncated":
        target.write_bytes(b"\x93NUMPY")
    elif damage == "archive":
        target.write_bytes(b"PK\x03\x04broken archive")
    else:
        vector = {
            "object": np.array(["pickle"], dtype=object),
            "dimension": np.ones(4, dtype=np.float32),
            "nan": np.full(384, np.nan, dtype=np.float32),
            "zero": np.zeros(384, dtype=np.float32),
            "dtype": np.ones(384, dtype=np.float64),
        }[damage]
        np.save(target, vector)
    recovered = memory.embed("recoverable")
    assert len(encoder[0].calls) == 2
    assert np.array_equal(recovered, expected)
    assert np.array_equal(memory.cache.load("recoverable"), expected)


def test_failed_write_preserves_published_file_and_cleans_temporary(tmp_path, monkeypatch):
    cache = EmbeddingCache(tmp_path, EmbeddingConfig())
    expected = np.ones(384, dtype=np.float32)
    cache.save("text", expected)
    original = cache.path("text").read_bytes()

    def interrupted(stream, *args, **kwargs):
        stream.write(b"partial file")
        raise OSError("disk unavailable")

    monkeypatch.setattr(np, "save", interrupted)
    cache.save("text", expected * 2)
    assert cache.path("text").read_bytes() == original
    assert list(tmp_path.glob("*.tmp")) == []


@pytest.mark.skipif(os.name != "nt", reason="Windows path spelling")
def test_cache_adapts_long_temporary_path_independently(tmp_path):
    root = tmp_path / ("c" * (225 - len(str(tmp_path)) - 70))
    cache = EmbeddingCache(root, EmbeddingConfig())
    assert 215 <= len(str(cache.path("text"))) < 248
    vector = np.ones(384, dtype=np.float32)
    cache.save("text", vector)
    assert np.array_equal(cache.load("text"), vector)
    assert not list(native_io_path(root).glob("*.tmp"))


@pytest.mark.skipif(os.name != "nt", reason="Windows resolver race")
def test_created_storage_does_not_persist_transient_extended_spelling(tmp_path, monkeypatch):
    workspace = tmp_path / "project"
    workspace.mkdir()
    storage = tmp_path / "storage"
    resolve = Path.resolve
    seen = []

    def raced(path, *args, **kwargs):
        if path == storage and (not seen):
            seen.append(path)
            return Path("\\\\?\\" + str(storage))
        return resolve(path, *args, **kwargs)

    monkeypatch.setattr(Path, "resolve", raced)
    assert trusted_state_root(workspace, storage) == storage
    assert seen and storage.is_dir()


def test_parallel_process_publication_never_returns_partial_vectors(tmp_path):
    cache = EmbeddingCache(tmp_path, EmbeddingConfig())
    cache.save("concurrent", np.ones(384, dtype=np.float32))
    code = "\nimport sys, numpy as np\nfrom agent_runtime.features.memory.embedding_cache import EmbeddingCache, EmbeddingConfig\ncache = EmbeddingCache(sys.argv[1], EmbeddingConfig())\nvector = np.full(384, float(sys.argv[2]), dtype=np.float32)\nfor _ in range(30):\n    cache.save('concurrent', vector)\n    result = cache.load('concurrent')\n    # Windows may temporarily deny opens during replace; this is a cache miss.\n    if result is not None:\n        assert result.shape == (384,) and (result == result[0]).all()\n"
    env = {**os.environ, "PYTHONPATH": str(Path(__file__).resolve().parents[1])}

    def write(value):
        return subprocess.run(
            [sys.executable, "-c", code, str(tmp_path), str(value)],
            env=env,
            capture_output=True,
            text=True,
            timeout=30,
        )

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(write, [2, 3]))
    for result in results:
        assert result.returncode == 0, result.stderr
    assert cache.load("concurrent")[0] in {2, 3}
    assert len(list(tmp_path.glob("*.npy"))) == 1
    assert not list(tmp_path.glob("*.tmp"))


def test_unscoped_indices_and_old_cache_do_not_persist_or_reuse(tmp_path, monkeypatch, encoder):
    monkeypatch.chdir(tmp_path)
    old_cache = tmp_path / ".agent" / "embed_cache"
    old_cache.mkdir(parents=True)
    digest = hashlib.sha256(b"legacy text").hexdigest()[:32]
    np.save(old_cache / f"{digest}.npy", np.ones(384, dtype=np.float32))
    before = list(tmp_path.rglob("*"))
    semantic.SemanticMemory().add({"text": "legacy text"})
    assert len(encoder[0].calls) == 1
    assert list(tmp_path.rglob("*")) == before
    scoped = semantic.SemanticMemory(cache_root=tmp_path / "external")
    scoped.embed("legacy text")
    assert len(encoder[1].calls) == 1


def test_retrieval_corpus_is_rebuilt_without_carrying_other_turns(tmp_path, encoder):
    memory = semantic.SemanticMemory(cache_root=tmp_path)
    memory.add({"text": "previous turn only", "memory_id": "old"})
    state = {"episodic_notes": [{"text": "current evidence", "memory_id": "current"}]}
    results = semantic.retrieval_candidates_semantic(
        state, "current evidence", semantic_memory=memory
    )
    assert {result["memory_id"] for result in results} <= {"current"}
    assert memory._notes[0]["memory_id"] == "old" and len(memory._notes) == 1


def test_model_loader_pins_revision_and_separates_profiles(monkeypatch):
    calls = []
    monkeypatch.setattr(semantic, "_SEMANTIC_MODELS", {})
    monkeypatch.setattr(semantic, "_configure_hf_hub", lambda _: None)

    def model(*args, **kwargs):
        calls.append((args, kwargs))
        return SimpleNamespace(max_seq_length=None)

    monkeypatch.setitem(
        sys.modules, "sentence_transformers", SimpleNamespace(SentenceTransformer=model)
    )
    config = EmbeddingConfig()
    first = semantic._get_semantic_model(config)
    assert semantic._get_semantic_model(config) is first
    second = semantic._get_semantic_model(replace(config, revision="b" * 40))
    third = semantic._get_semantic_model(replace(config, max_seq_length=128))
    assert first is not second and first is not third
    assert len(calls) == 3
    assert calls[0][1] == {"revision": config.revision, "device": "cpu", "trust_remote_code": False}
    assert first.max_seq_length == 256 and third.max_seq_length == 128


@pytest.mark.parametrize("revision", ["main", "", "latest"])
def test_mutable_model_version_rejected(revision):
    with pytest.raises(ValueError, match="immutable revision"):
        EmbeddingConfig(revision=revision)


def test_relative_cache_or_project_paths_rejected():
    with pytest.raises(ValueError, match="absolute cache root"):
        EmbeddingCache(".agent/cache", EmbeddingConfig())
    with pytest.raises(ValueError, match="absolute project root"):
        project_embedding_cache_root("relative-repo")


def test_agents_in_copies_share_source_cache_but_not_corpus(tmp_path, monkeypatch, encoder):
    from agent_runtime.config import AgentConfig
    from agent_runtime.providers.clients import FakeModelClient
    from agent_runtime.runtime import Agent
    from agent_runtime.tool_context import ToolContext
    from agent_runtime.workspace import WorkspaceContext

    source, other, storage = (tmp_path / name for name in ("source", "other", "storage"))
    source.mkdir()
    other.mkdir()

    def agent(name, project):
        root = tmp_path / name
        root.mkdir()
        return Agent(
            AgentConfig(provider="fake"),
            FakeModelClient([]),
            WorkspaceContext(cwd=str(root), repo_root=str(root), doc_contents={}),
            tool_context=ToolContext(
                root=str(root), project_root=str(project), embedding_state_base=str(storage)
            ),
        )

    first, second, third = (agent("run-a", source), agent("run-b", source), agent("run-c", other))
    monkeypatch.setenv("FIXLOOP_STATE_ROOT", str(tmp_path / "another-state"))
    monkeypatch.chdir(other)
    first.semantic_memory.add({"text": "same project data"})
    second.semantic_memory.add({"text": "same project data"})
    third.semantic_memory.add({"text": "same project data"})
    assert first.semantic_memory.cache.root == second.semantic_memory.cache.root
    assert first.semantic_memory.cache.root != third.semantic_memory.cache.root
    assert len(encoder[0].calls) == 1 and (not encoder[1].calls) and (len(encoder[2].calls) == 1)
    assert first.semantic_memory._notes is not second.semantic_memory._notes
    assert not (other / ".agent").exists()


def test_interactive_project_switch_resets_corpus_and_changes_cache(tmp_path, monkeypatch, encoder):
    from agent_runtime.config import AgentConfig
    from agent_runtime.providers.clients import FakeModelClient
    from agent_runtime.runtime import Agent
    from agent_runtime.workspace import WorkspaceContext

    first, second = (tmp_path / "first", tmp_path / "second")
    first.mkdir()
    second.mkdir()
    monkeypatch.setenv("FIXLOOP_STATE_ROOT", str(tmp_path / "storage"))
    monkeypatch.chdir(first)
    agent = Agent(AgentConfig(provider="fake"), FakeModelClient([]), WorkspaceContext.build(first))
    agent._detect_workspace_switch()
    previous = agent.semantic_memory
    previous.add({"text": "first project data"})
    monkeypatch.chdir(second)
    agent._detect_workspace_switch()
    current = agent.semantic_memory
    assert current is not previous and (not current._notes)
    assert current.cache.root != previous.cache.root
    assert project_id(second) in current.cache.root.parts
    current.embed("first project data")
    assert len(encoder[1].calls) == 1


def test_host_worktree_rebind_keeps_shared_cache_and_resets_corpus(
    temp_workspace, tmp_path, encoder
):
    from agent_runtime.providers.clients import FakeModelClient
    from src.repair.assembly import create_repair_service
    from src.state import RepairState

    storage = tmp_path / "storage"
    orch = create_repair_service(
        FakeModelClient([]), str(temp_workspace), skip_verify=True, state_root=str(storage)
    ).session
    original = orch.patcher.semantic_memory
    original.add({"text": "candidate text"})
    state = RepairState(issue_input="fix", repair_run_id="cache-bind")
    orch.lifecycle.prepare_host_worktree(state, resume=False)
    rebound = orch.patcher.semantic_memory
    assert rebound is not original and (not rebound._notes)
    assert rebound.cache.root == original.cache.root
    assert not rebound.cache.root.is_relative_to(Path(orch.patcher.state_root))
    rebound.add({"text": "candidate text"})
    assert not encoder[1].calls


def test_sandbox_binding_and_resume_preserve_source_cache(temp_workspace, tmp_path, encoder):
    from agent_runtime.config import AgentConfig
    from agent_runtime.linux_sandbox.task_context import SandboxTaskBinding, SandboxTaskSpec
    from agent_runtime.providers.clients import FakeModelClient
    from agent_runtime.runtime import Agent
    from agent_runtime.tool_context import ToolContext
    from agent_runtime.workspace import WorkspaceContext
    from tests.test_repair_factory import _prepared_environment
    from tests.test_sandbox_task_context import metadata_backend

    storage = tmp_path / "storage"
    copy = tmp_path / "copy"
    copy.mkdir()
    spec = SandboxTaskSpec(
        _prepared_environment().identity, "cache-v1", "a" * 64, str(storage), str(temp_workspace)
    )
    binding = SandboxTaskBinding.create(copy, copy, "task", spec)
    binding.save()
    for current in (binding, SandboxTaskBinding.load(copy, "task", spec)):
        context = ToolContext(root=str(copy), sandbox_mode=True, embedding_state_base=str(storage))
        agent = Agent(
            AgentConfig(provider="fake"),
            FakeModelClient([]),
            WorkspaceContext(cwd=str(copy), repo_root=str(copy), doc_contents={}),
            tool_context=context,
        )
        agent.bind_sandbox_task(current, metadata_backend(current))
        assert agent.tool_context.project_root == str(temp_workspace)
        assert project_id(temp_workspace) in agent.semantic_memory.cache.root.parts
        assert not agent.semantic_memory.cache.root.is_relative_to(Path(binding.state_root))
        agent.semantic_memory.embed("restored input")
    assert len(encoder[0].calls) == 1 and (not encoder[1].calls)


def test_project_cache_write_failure_keeps_semantic_retrieval(tmp_path, monkeypatch, encoder):
    memory = semantic.SemanticMemory(cache_root=tmp_path / "cache")

    def denied(*args, **kwargs):
        raise PermissionError("cache optional")

    monkeypatch.setattr(Path, "mkdir", denied)
    memory.add({"text": "in-memory note", "memory_id": "note"})
    assert memory.search("in-memory note")[0]["memory_id"] == "note"
    assert not memory.cache.root.exists()


@pytest.mark.parametrize("mode", ["project_chat", "repair"])
def test_workbench_copies_use_source_cache_outside_run_state(tmp_path, monkeypatch, encoder, mode):
    from contextlib import closing

    from agent_runtime.cancellation import CancellationToken
    from agent_runtime.providers.clients import FakeModelClient
    from agent_runtime.runtime import Agent
    from src.repair.assembly import create_repair_service
    from src.state import RepairState, VerificationResult
    from src.workbench.files import ProjectFiles
    from src.workbench.project import ProjectExecutor

    source, storage = (tmp_path / "source", tmp_path / "storage")
    source.mkdir()
    monkeypatch.setenv("FIXLOOP_STATE_ROOT", str(storage))
    memories = []

    def encode(agent):
        memories.append(agent.semantic_memory)
        agent.semantic_memory.embed("shared derived text")

    def ask(agent, *args, **kwargs):
        encode(agent)
        agent.last_task_state = SimpleNamespace(stop_reason="final")
        return "done"

    monkeypatch.setattr(Agent, "ask", ask)

    def factory(client, root, **kwargs):
        orch = create_repair_service(client, root, skip_verify=True, **kwargs).session

        def repair(*args, **kwargs):
            encode(orch.patcher)
            return RepairState(
                issue_input="fix",
                status="fixed",
                verification_result=VerificationResult(all_passed=True),
            )

        orch.lifecycle.execute = repair
        return orch

    executor = ProjectExecutor(
        lambda: FakeModelClient([]), provider="fake", execution_tier="host", repair_factory=factory
    )
    for run_id in ("first", "second"):
        copy, state = (tmp_path / run_id, tmp_path / "runs" / run_id)
        copy.mkdir()
        with closing(ProjectFiles(copy)) as files:
            result = executor(
                mode=mode,
                files=files,
                messages=[{"role": "user", "content": "question"}],
                token=CancellationToken(),
                on_chunk=lambda _: None,
                publish=lambda *a: None,
                run_id=run_id,
                state_root=state,
                project_root=str(source),
            )
        assert result.status == "succeeded"
        assert not memories[-1].cache.root.is_relative_to(state)
        assert memories[-1].cache.root == project_embedding_cache_root(source, storage)
    assert memories[0] is not memories[1]
    assert len(encoder[0].calls) == 1 and (not encoder[1].calls)
