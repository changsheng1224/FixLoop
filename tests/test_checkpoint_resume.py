"""Checkpoint + Security + Resume 集成测试。"""

import tempfile

import pytest

from agent_runtime.checkpoint import (
    create_checkpoint,
    current_runtime_identity,
    evaluate_resume_state,
)
from agent_runtime.config import AgentConfig
from agent_runtime.providers.clients import FakeModelClient
from agent_runtime.runtime import Agent
from agent_runtime.security import redact_artifact
from agent_runtime.session_store import SessionStore
from agent_runtime.task_state import TaskState
from agent_runtime.workspace import WorkspaceContext


@pytest.fixture
def agent(temp_workspace):
    config = AgentConfig(provider="fake", max_steps=3)
    ws = WorkspaceContext.build(str(temp_workspace))
    client = FakeModelClient(["<final>ok</final>"])
    return Agent(config=config, model_client=client, workspace=ws)


class TestCheckpoint:
    """checkpoint 创建与恢复测试。"""

    def test_create_checkpoint(self, agent):
        ts = TaskState.create(user_request="fix bug")
        cp = create_checkpoint(agent, ts, "fix bug")
        assert cp["schema_version"] == "1.0"
        assert cp["current_goal"] == "fix bug"
        assert "key_files" in cp

    def test_current_runtime_identity(self, agent):
        ident = current_runtime_identity(agent)
        assert ident["provider"] == "fake"
        assert ident["model"] == "deepseek-v4-pro"

    def test_evaluate_no_checkpoint(self, agent):
        result = evaluate_resume_state(agent)
        assert result["status"] == "no-checkpoint"

    def test_evaluate_full_valid(self, agent):
        ts = TaskState.create(user_request="test")
        create_checkpoint(agent, ts, "test")
        result = evaluate_resume_state(agent)
        assert result["status"] == "full-valid"

    def test_evaluate_stale_file(self, agent, temp_workspace):
        # 先让 agent 读一个文件
        agent.update_memory_after_tool("read_file", {"path": "README.md"}, "old")
        ts = TaskState.create(user_request="test")
        create_checkpoint(agent, ts, "test")

        # 修改文件
        (temp_workspace / "README.md").write_text("modified content")

        result = evaluate_resume_state(agent)
        assert result["status"] == "partial-stale"
        assert "README.md" in result["stale_files"]

    def test_evaluate_identity_mismatch(self, agent):
        ts = TaskState.create(user_request="test")
        create_checkpoint(agent, ts, "test")

        # 修改 identity
        agent.config.max_steps = 10
        result = evaluate_resume_state(agent)
        assert result["status"] == "workspace-mismatch"


class TestRedactArtifact:
    """redact_artifact 脱敏测试。"""

    def test_redact_dict_key(self):
        data = {"DEEPSEEK_API_KEY": "sk-1234567890abcdef"}
        result = redact_artifact(data)
        assert result["DEEPSEEK_API_KEY"] == "<redacted>"

    def test_redact_nested(self):
        data = {
            "config": {"api_key": "sk-secret"},
            "env": {"PATH": "/usr/bin", "TOKEN": "ghp_secret1234567890"},
        }
        result = redact_artifact(data, secret_values=["sk-secret", "ghp_secret1234567890"])
        assert result["config"]["api_key"] == "<redacted>"
        assert result["env"]["PATH"] == "/usr/bin"

    def test_redact_list(self):
        data = [
            {"K": "ok"},
            {"SECRET_KEY": "my-password"},
        ]
        result = redact_artifact(data, secret_values=["my-password"])
        assert result[0]["K"] == "ok"
        assert result[1]["SECRET_KEY"] == "<redacted>"

    def test_redact_str_value(self):
        text = "Using token sk-1234567890abcdef for auth"
        result = redact_artifact(text, secret_values=["sk-1234567890abcdef"])
        assert "sk-1234567890abcdef" not in result
        assert "<redacted>" in result


class TestSessionResume:
    """Session 恢复测试。"""

    def test_from_session_restores_history(self, temp_workspace):
        ws = WorkspaceContext.build(str(temp_workspace))
        config = AgentConfig(provider="fake")

        with tempfile.TemporaryDirectory() as tmpdir:
            store = SessionStore(root=tmpdir)
            # 保存 session
            store.save(
                {
                    "id": "s1",
                    "history": [
                        {"role": "user", "content": "hello"},
                        {"role": "assistant", "content": "hi there"},
                    ],
                }
            )

            client = FakeModelClient(["<final>ok</final>"])
            agent = Agent.from_session(client, ws, store, "s1", config=config)
            assert agent is not None
            assert len(agent.session["history"]) == 2
            assert agent.session["history"][0]["content"] == "hello"

    def test_from_session_nonexistent(self, temp_workspace):
        ws = WorkspaceContext.build(str(temp_workspace))
        with tempfile.TemporaryDirectory() as tmpdir:
            store = SessionStore(root=tmpdir)
            client = FakeModelClient(["<final>ok</final>"])
            agent = Agent.from_session(client, ws, store, "ghost")
            assert agent is None
