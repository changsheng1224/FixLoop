"""Provider JSON mode 全角色单测：表驱动 + 降级。"""

from src.agents.factory import create_repair_agent


class TestJsonModeAllRoles:
    def test_localizer_has_json_mode(self, tmp_path):
        from agent_runtime.providers.clients import FakeModelClient
        from agent_runtime.workspace import WorkspaceContext

        ws = WorkspaceContext.build(str(tmp_path))
        client = FakeModelClient(["[]"])
        agent = create_repair_agent("localizer", client, ws, cwd=str(tmp_path))
        assert agent.config.json_mode is True

    def test_retriever_has_json_mode(self, tmp_path):
        from agent_runtime.providers.clients import FakeModelClient
        from agent_runtime.workspace import WorkspaceContext

        ws = WorkspaceContext.build(str(tmp_path))
        client = FakeModelClient(["{}"])
        agent = create_repair_agent("retriever", client, ws, cwd=str(tmp_path))
        assert agent.config.json_mode is True

    def test_patcher_has_json_mode(self, tmp_path):
        from agent_runtime.providers.clients import FakeModelClient
        from agent_runtime.workspace import WorkspaceContext

        ws = WorkspaceContext.build(str(tmp_path))
        client = FakeModelClient(["{}"])
        agent = create_repair_agent("patcher", client, ws, cwd=str(tmp_path))
        assert agent.config.json_mode is True

    def test_verifier_has_json_mode(self, tmp_path):
        from agent_runtime.providers.clients import FakeModelClient
        from agent_runtime.workspace import WorkspaceContext

        ws = WorkspaceContext.build(str(tmp_path))
        client = FakeModelClient(["{}"])
        agent = create_repair_agent("verifier", client, ws, cwd=str(tmp_path))
        assert agent.config.json_mode is True

    def test_baseline_has_no_json_mode(self, tmp_path):
        """baseline 单 Agent 变体不启用 JSON mode。"""
        from agent_runtime.providers.clients import FakeModelClient
        from agent_runtime.workspace import WorkspaceContext

        ws = WorkspaceContext.build(str(tmp_path))
        client = FakeModelClient(["final answer"])
        agent = create_repair_agent("baseline", client, ws, cwd=str(tmp_path))
        assert agent.config.json_mode is False

    def test_all_roles_have_format_hint(self, tmp_path):
        """四角色 system prompt 含 JSON 格式提示。"""
        from agent_runtime.providers.clients import FakeModelClient
        from agent_runtime.workspace import WorkspaceContext

        ws = WorkspaceContext.build(str(tmp_path))
        client = FakeModelClient(["{}"])

        for role in ("localizer", "retriever", "patcher", "verifier"):
            agent = create_repair_agent(role, client, ws, cwd=str(tmp_path))
            assert "输出格式" in agent._system_prompt or "JSON" in agent._system_prompt
