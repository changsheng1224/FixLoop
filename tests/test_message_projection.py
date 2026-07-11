"""多轮 messages 前缀对齐（Scheme C Phase 1）单测。"""

from __future__ import annotations

import copy

import pytest

from agent_runtime.config import AgentConfig
from agent_runtime.context_manager import ContextManager
from agent_runtime.message_projection import (
    attach_projection_metadata,
    build_context_prefix,
    check_prefix_aligned,
    init_run_projection,
    seal_history_at_build,
)
from agent_runtime.providers.clients import FakeModelClient
from agent_runtime.runtime import Agent
from agent_runtime.workspace import WorkspaceContext


@pytest.fixture
def agent(temp_workspace):
    config = AgentConfig(provider="fake", max_steps=5, prompt_budget=6000)
    ws = WorkspaceContext.build(str(temp_workspace))
    client = FakeModelClient(["<final>ok</final>"])
    return Agent(config=config, model_client=client, workspace=ws)


class TestMessageProjectionHelpers:
    def test_check_prefix_aligned_first_step(self):
        assert check_prefix_aligned("", "any prefix") is True

    def test_check_prefix_aligned_growing_prefix(self):
        prev = "stable|memory|history-v1"
        curr = "stable|memory|history-v1|history-v2"
        assert check_prefix_aligned(prev, curr) is True

    def test_check_prefix_aligned_divergent(self):
        prev = "stable|memory|history-v1"
        curr = "stable|memory|HISTORY-REWRITTEN|history-v2"
        assert check_prefix_aligned(prev, curr) is False

    def test_init_run_projection_freezes_memory_and_query(self, agent):
        agent.session["memory"]["working"]["recent_files"] = ["a.py"]
        init_run_projection(agent.session, "find bug in a.py")
        agent.session["memory"]["working"]["recent_files"] = ["b.py"]
        assert agent.session["_run_user_query"] == "find bug in a.py"
        assert agent.session["_run_memory_snapshot"]["working"]["recent_files"] == ["a.py"]

    def test_seal_history_at_build(self, agent):
        init_run_projection(agent.session, "q")
        seal_history_at_build(agent.session, 3, "## 对话历史\n\n**user**: hi")
        assert agent.session["_sealed_history_count"] == 3
        assert agent.session["_sealed_history_text"].startswith("## 对话历史")


class TestContextManagerSealedHistory:
    def test_history_prefix_grows_monotonically_across_builds(self, agent):
        init_run_projection(agent.session, "analyze repo")
        agent.session["history"] = [{"role": "user", "content": "analyze repo", "turn_id": 1}]
        cm = ContextManager(agent)

        _, meta1 = cm.build("analyze repo")
        h1 = meta1["sections"].get("history", 0)
        assert h1 > 0

        agent.session["history"].extend(
            [
                {"role": "assistant", "content": "call list_files", "turn_id": 1},
                {"role": "tool", "content": "file1.py\nfile2.py", "turn_id": 1},
            ]
        )
        _, meta2 = cm.build("tool result step 1")
        prefix1 = build_context_prefix(agent, meta1)
        prefix2 = build_context_prefix(agent, meta2)
        assert check_prefix_aligned(prefix1, prefix2)

        agent.session["history"].extend(
            [
                {"role": "assistant", "content": "call read_file", "turn_id": 1},
                {"role": "tool", "content": "def foo(): pass", "turn_id": 1},
            ]
        )
        _, meta3 = cm.build("tool result step 2")
        prefix3 = build_context_prefix(agent, meta3)
        attach_projection_metadata(meta3, agent.session, context_prefix=prefix3)
        assert check_prefix_aligned(prefix2, prefix3)
        assert meta3.get("prefix_aligned") is True

    def test_memory_snapshot_prevents_prefix_drift_from_recent_files(self, agent):
        init_run_projection(agent.session, "scan")
        snap = copy.deepcopy(agent.session["_run_memory_snapshot"])
        snap["working"]["recent_files"] = []
        agent.session["_run_memory_snapshot"] = snap

        agent.session["history"] = [{"role": "user", "content": "scan", "turn_id": 1}]
        cm = ContextManager(agent)
        _, meta1 = cm.build("scan")
        prefix1 = build_context_prefix(agent, meta1)

        agent.update_memory_after_tool("read_file", {"path": "x.py"}, "content")
        agent.session["history"].append(
            {"role": "assistant", "content": "read x.py", "turn_id": 1}
        )
        _, meta2 = cm.build("done reading")
        prefix2 = build_context_prefix(agent, meta2)
        assert check_prefix_aligned(prefix1, prefix2)


class TestAttachProjectionMetadata:
    def test_attach_sets_prefix_aligned_and_step(self, agent):
        init_run_projection(agent.session, "hello")
        meta = {"sections": {"history": 10}, "prompt_cache_key": "abc"}
        attach_projection_metadata(meta, agent.session, context_prefix="part-a")
        assert meta["projection_step"] == 1
        assert meta["prefix_aligned"] is True
        assert meta["prefix_fingerprint"]

        attach_projection_metadata(meta, agent.session, context_prefix="part-a|part-b")
        assert meta["projection_step"] == 2
        assert meta["prefix_aligned"] is True
