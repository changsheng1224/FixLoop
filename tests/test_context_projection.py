"""八段 Context 投影 schema 单测。"""

import pytest

from agent_runtime.config import AgentConfig
from agent_runtime.context_manager import ContextManager, TokenBudget, fit_prompt_to_budget
from agent_runtime.context_projection import (
    CONTEXT_SCHEMA_VERSION,
    EIGHT_SECTIONS,
    build_context_sections,
    empty_context_sections,
    split_stable_text,
)
from agent_runtime.providers.clients import FakeModelClient
from agent_runtime.runtime import Agent
from agent_runtime.workspace import WorkspaceContext


@pytest.fixture
def agent(temp_workspace):
    config = AgentConfig(provider="fake", max_steps=4, prompt_budget=6000)
    ws = WorkspaceContext.build(str(temp_workspace))
    client = FakeModelClient(["<final>ok</final>"])
    return Agent(config=config, model_client=client, workspace=ws)


class TestSplitStableText:
    def test_splits_tools_and_examples(self):
        stable = "persona\n\n## 可用工具\n\nlist_files\n\n## 调用示例\n\nex"
        core, tools, examples = split_stable_text(stable)
        assert "persona" in core
        assert "list_files" in tools
        assert "ex" in examples

    def test_no_markers_returns_core_only(self):
        core, tools, examples = split_stable_text("only rules")
        assert core == "only rules"
        assert tools == ""
        assert examples == ""


class TestBuildContextSections:
    def test_all_eight_keys_present(self, agent):
        budget = TokenBudget(provider="fake", total_limit=6000)
        cm = ContextManager(agent)
        _, meta = cm.build("hello task")
        ctx = build_context_sections(meta["sections"], agent=agent, budget=budget)
        assert set(ctx.keys()) == set(EIGHT_SECTIONS)

    def test_task_maps_from_request(self, agent):
        budget = TokenBudget(provider="fake", total_limit=6000)
        cm = ContextManager(agent)
        _, meta = cm.build("my user task")
        ctx = build_context_sections(meta["sections"], agent=agent, budget=budget)
        assert ctx["task"] == meta["sections"]["request"]
        assert ctx["task"] > 0

    def test_knowledge_maps_from_relevant(self, agent):
        agent.session.setdefault("memory", {})["episodic"] = [
            {"text": "prior fix for import error in utils", "score": 0.9}
        ]
        budget = TokenBudget(provider="fake", total_limit=6000)
        cm = ContextManager(agent)
        _, meta = cm.build("import error in utils")
        ctx = build_context_sections(meta["sections"], agent=agent, budget=budget)
        if meta["sections"].get("relevant"):
            assert ctx["knowledge"] == meta["sections"]["relevant"]

    def test_state_zero_without_plan_todos(self, agent):
        budget = TokenBudget(provider="fake", total_limit=6000)
        cm = ContextManager(agent)
        _, meta = cm.build("hello")
        ctx = build_context_sections(meta["sections"], agent=agent, budget=budget)
        assert ctx["state"] == 0

    def test_state_counts_plan_todos(self, agent):
        agent.session["plan_todos"] = [{"id": "1", "content": "fix import", "status": "pending"}]
        budget = TokenBudget(provider="fake", total_limit=6000)
        cm = ContextManager(agent)
        _, meta = cm.build("hello")
        ctx = build_context_sections(meta["sections"], agent=agent, budget=budget)
        assert ctx["state"] > 0

    def test_tools_positive_when_prefix_has_tools(self, agent):
        budget = TokenBudget(provider="fake", total_limit=6000)
        cm = ContextManager(agent)
        _, meta = cm.build("hello")
        ctx = build_context_sections(meta["sections"], agent=agent, budget=budget)
        assert ctx["tools"] > 0
        assert ctx["system"] > 0


class TestAttachContextProjection:
    def test_metadata_dual_write(self, agent):
        cm = ContextManager(agent)
        _, meta = cm.build("dual write test")
        assert meta["context_schema_version"] == CONTEXT_SCHEMA_VERSION
        assert "context_sections" in meta
        assert set(meta["context_sections"].keys()) == set(EIGHT_SECTIONS)
        assert meta["context_sections_total"] == sum(meta["context_sections"].values())
        assert "request" in meta["sections"]

    def test_fit_prompt_to_budget_has_context_sections(self):
        _, _, meta = fit_prompt_to_budget("sys", "user task", total_limit=6000)
        assert meta["context_sections"]["system"] > 0
        assert meta["context_sections"]["task"] > 0
        assert meta["context_sections"]["memory"] == 0


class TestEmptyContextSections:
    def test_all_zero(self):
        ctx = empty_context_sections()
        assert len(ctx) == 8
        assert sum(ctx.values()) == 0
