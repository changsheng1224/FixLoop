"""Task / user message 永不压缩 enforce 单测。"""

import pytest

from agent_runtime.config import AgentConfig
from agent_runtime.context_manager import (
    ContextManager,
    fit_prompt_to_budget,
    fit_repair_user_prompt,
)
from agent_runtime.providers.clients import FakeModelClient
from agent_runtime.runtime import Agent
from agent_runtime.task_preservation import issue_preserved, reserve_section_budget
from agent_runtime.workspace import WorkspaceContext

ISSUE_MARKER = "UNIQUE_ISSUE_MARKER_XYZ_42"


@pytest.fixture
def agent(temp_workspace):
    config = AgentConfig(provider="fake", max_steps=4, prompt_budget=6000)
    ws = WorkspaceContext.build(str(temp_workspace))
    client = FakeModelClient(["<final>ok</final>"])
    return Agent(config=config, model_client=client, workspace=ws)


class TestReserveSectionBudget:
    def test_subtracts_request_tokens(self):
        assert reserve_section_budget(6000, 500) == 5500

    def test_never_negative(self):
        assert reserve_section_budget(100, 200) == 0


class TestContextManagerTaskPreservation:
    def test_issue_preserved_under_tiny_budget(self, agent):
        issue = f"{ISSUE_MARKER} " + "detail " * 200
        cm = ContextManager(agent, total_budget=400)
        prompt, meta = cm.build(issue)

        assert meta.get("request_preserved") is True
        assert issue_preserved(issue, prompt)
        assert ISSUE_MARKER in prompt
        assert not any("裁剪 request" in cut for cut in meta.get("cuts", []))

    def test_long_history_does_not_truncate_current_task(self, agent):
        for i in range(30):
            agent.record({"role": "user", "content": f"question {i}"})
            agent.record({"role": "tool", "content": f"result {i}: " + "x" * 300})
        issue = f"{ISSUE_MARKER} final ask"
        cm = ContextManager(agent)
        prompt, meta = cm.build(issue)

        assert meta.get("request_preserved") is True
        assert ISSUE_MARKER in prompt
        assert issue_preserved(issue, prompt)

    def test_request_section_never_cut_metadata(self, agent):
        cm = ContextManager(agent, total_budget=300)
        _, meta = cm.build("hello preserved task")
        assert meta.get("request_preserved") is True
        assert "request" in meta["sections"]


class TestFitPromptPreserveUser:
    def test_user_text_fully_preserved(self):
        user = "preserve-me " * 800
        system = "system " * 400
        _, fitted_user, meta = fit_prompt_to_budget(
            system,
            user,
            total_limit=600,
            preserve_user=True,
        )
        assert fitted_user == user
        assert meta.get("request_preserved") is True
        assert len(fitted_user) == len(user)

    def test_system_trimmed_instead_of_user(self):
        user = "user " * 100
        system = "system " * 2000
        fitted_system, fitted_user, meta = fit_prompt_to_budget(
            system,
            user,
            total_limit=600,
            preserve_user=True,
        )
        assert fitted_user == user
        assert len(fitted_system) < len(system)
        assert any("保留 user" in cut for cut in meta["cuts"])

    def test_task_budget_overflow_when_user_exceeds_total(self):
        user = "overflow " * 5000
        _, fitted_user, meta = fit_prompt_to_budget(
            "sys",
            user,
            total_limit=100,
            preserve_user=True,
        )
        assert fitted_user == user
        assert meta.get("task_budget_overflow") is True
        assert meta["total_tokens"] > meta["budget"]

    def test_legacy_fit_can_still_truncate_user(self):
        user = "truncate " * 5000
        _, fitted_user, meta = fit_prompt_to_budget(
            "sys",
            user,
            total_limit=600,
            preserve_user=False,
        )
        assert len(fitted_user) < len(user)
        assert "request_preserved" not in meta


class TestFitRepairUserPrompt:
    def test_repair_issue_preserved(self, temp_workspace):
        config = AgentConfig(
            provider="openai",
            model="gpt-4",
            max_steps=1,
            prompt_budget=800,
        )
        ws = WorkspaceContext.build(str(temp_workspace))
        agent = Agent(config=config, model_client=FakeModelClient([]), workspace=ws)
        issue_block = f"{ISSUE_MARKER}\n" + "stack trace line\n" * 400
        fitted, meta = fit_repair_user_prompt(agent, issue_block, "system " * 500)
        assert meta.get("request_preserved") is True
        assert ISSUE_MARKER in fitted
        assert issue_preserved(ISSUE_MARKER, fitted)
