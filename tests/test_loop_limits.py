"""Configured limits stop execution and expose the exhausted resource."""

from agent_runtime.config import AgentConfig
from agent_runtime.config_loader import load_runtime_policy
from agent_runtime.providers.clients import FakeModelClient
from agent_runtime.runtime import Agent
from agent_runtime.workspace import WorkspaceContext


def test_configured_model_attempts_stop_malformed_output(tmp_path):
    client = FakeModelClient(["malformed"] * 10)
    config = AgentConfig(provider="fake", max_steps=50, max_model_attempts=2)
    agent = Agent(config, client, WorkspaceContext.build(str(tmp_path)), cwd=str(tmp_path))
    result = agent.ask("inspect", skip_plan=True)
    assert "max_model_attempts=2" in result
    assert len(client.prompts) == 2


def test_environment_model_attempts_have_provenance(tmp_path, monkeypatch):
    monkeypatch.setenv("FIXLOOP_MAX_MODEL_ATTEMPTS", "5")
    config = load_runtime_policy(workspace_root=str(tmp_path))
    assert config.max_model_attempts == 5
    assert "env" in config.snapshot()["provenance"]["max_model_attempts"]
