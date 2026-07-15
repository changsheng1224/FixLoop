"""Checkpoint 触发点规范单测：三 trigger + 非法拒写。"""

import pytest


class TestCheckpointTrigger:
    def test_valid_triggers_accepted(self, temp_workspace):
        from agent_runtime.checkpoint import VALID_TRIGGERS, create_checkpoint
        from agent_runtime.config import AgentConfig
        from agent_runtime.providers.clients import FakeModelClient
        from agent_runtime.runtime import Agent
        from agent_runtime.task_state import TaskState
        from agent_runtime.workspace import WorkspaceContext

        ws = WorkspaceContext.build(str(temp_workspace))
        agent = Agent(
            config=AgentConfig(provider="fake"),
            model_client=FakeModelClient([]),
            workspace=ws,
            cwd=str(temp_workspace),
        )
        ts = TaskState.create(user_request="test")
        for trigger in sorted(VALID_TRIGGERS):
            cp = create_checkpoint(agent, ts, "test", trigger=trigger)
            assert cp["trigger"] == trigger

    def test_invalid_trigger_raises(self, temp_workspace):
        from agent_runtime.checkpoint import create_checkpoint
        from agent_runtime.config import AgentConfig
        from agent_runtime.providers.clients import FakeModelClient
        from agent_runtime.runtime import Agent
        from agent_runtime.task_state import TaskState
        from agent_runtime.workspace import WorkspaceContext

        ws = WorkspaceContext.build(str(temp_workspace))
        agent = Agent(
            config=AgentConfig(provider="fake"),
            model_client=FakeModelClient([]),
            workspace=ws,
            cwd=str(temp_workspace),
        )
        ts = TaskState.create(user_request="test")
        with pytest.raises(ValueError, match="非法 trigger"):
            create_checkpoint(agent, ts, "test", trigger="manual")

    def test_step_end_has_last_tool(self, temp_workspace):
        from agent_runtime.checkpoint import create_checkpoint
        from agent_runtime.config import AgentConfig
        from agent_runtime.providers.clients import FakeModelClient
        from agent_runtime.runtime import Agent
        from agent_runtime.task_state import TaskState
        from agent_runtime.workspace import WorkspaceContext

        ws = WorkspaceContext.build(str(temp_workspace))
        agent = Agent(
            config=AgentConfig(provider="fake"),
            model_client=FakeModelClient([]),
            workspace=ws,
            cwd=str(temp_workspace),
        )
        ts = TaskState.create(user_request="test")
        cp = create_checkpoint(agent, ts, "test", trigger="step_end", last_tool="read_file")
        assert cp["last_tool"] == "read_file"

    def test_user_cancel_has_in_flight_tool(self, temp_workspace):
        from agent_runtime.checkpoint import create_checkpoint
        from agent_runtime.config import AgentConfig
        from agent_runtime.providers.clients import FakeModelClient
        from agent_runtime.runtime import Agent
        from agent_runtime.task_state import TaskState
        from agent_runtime.workspace import WorkspaceContext

        ws = WorkspaceContext.build(str(temp_workspace))
        agent = Agent(
            config=AgentConfig(provider="fake"),
            model_client=FakeModelClient([]),
            workspace=ws,
            cwd=str(temp_workspace),
        )
        ts = TaskState.create(user_request="test")
        cp = create_checkpoint(
            agent,
            ts,
            "test",
            trigger="user_cancel",
            in_flight_tool="write_file",
        )
        assert cp["in_flight_tool"] == "write_file"

    def test_ask_end_no_in_flight(self, temp_workspace):
        from agent_runtime.checkpoint import create_checkpoint
        from agent_runtime.config import AgentConfig
        from agent_runtime.providers.clients import FakeModelClient
        from agent_runtime.runtime import Agent
        from agent_runtime.task_state import TaskState
        from agent_runtime.workspace import WorkspaceContext

        ws = WorkspaceContext.build(str(temp_workspace))
        agent = Agent(
            config=AgentConfig(provider="fake"),
            model_client=FakeModelClient([]),
            workspace=ws,
            cwd=str(temp_workspace),
        )
        ts = TaskState.create(user_request="test")
        cp = create_checkpoint(agent, ts, "test", trigger="ask_end")
        assert "in_flight_tool" not in cp
        assert cp["last_tool"] == ""
