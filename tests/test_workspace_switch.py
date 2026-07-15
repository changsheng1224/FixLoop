"""Workspace 切换检测单测：cwd/root_hash 变更 → prefix rebuild + recent_files clear。"""


class TestWorkspaceSwitchDetection:
    def test_first_call_records_baseline(self, temp_workspace):
        """首次调用记录 cwd 和 root_hash 基线。"""
        from agent_runtime.config import AgentConfig
        from agent_runtime.providers.clients import FakeModelClient
        from agent_runtime.runtime import Agent
        from agent_runtime.workspace import WorkspaceContext

        ws = WorkspaceContext.build(str(temp_workspace))
        agent = Agent(
            config=AgentConfig(provider="fake"),
            model_client=FakeModelClient([]),
            workspace=ws,
            cwd=str(temp_workspace),
        )
        agent._detect_workspace_switch()
        assert agent._last_cwd is not None
        assert agent._last_root_hash is not None

    def test_same_cwd_no_rebuild(self, temp_workspace):
        """同 cwd + 同 hash → 不重建。"""
        from agent_runtime.config import AgentConfig
        from agent_runtime.providers.clients import FakeModelClient
        from agent_runtime.runtime import Agent
        from agent_runtime.workspace import WorkspaceContext

        ws = WorkspaceContext.build(str(temp_workspace))
        agent = Agent(
            config=AgentConfig(provider="fake"),
            model_client=FakeModelClient([]),
            workspace=ws,
            cwd=str(temp_workspace),
        )
        agent._detect_workspace_switch()
        old_prefix = agent._prefix
        # 再次检测 → 无变更
        agent._detect_workspace_switch()
        # prefix 不应重建（同 hash）
        assert agent._prefix is old_prefix

    def test_switch_clears_recent_files(self, temp_workspace):
        """cwd 变更后清空 recent_files。"""
        from agent_runtime.config import AgentConfig
        from agent_runtime.providers.clients import FakeModelClient
        from agent_runtime.runtime import Agent
        from agent_runtime.workspace import WorkspaceContext

        ws = WorkspaceContext.build(str(temp_workspace))
        agent = Agent(
            config=AgentConfig(provider="fake"),
            model_client=FakeModelClient([]),
            workspace=ws,
            cwd=str(temp_workspace),
        )
        mem = agent.session.setdefault("memory", {})
        mem.setdefault("working", {})["recent_files"] = ["old_file.py"]
        agent._detect_workspace_switch()
        # 首次 → 不清理
        working = agent.session["memory"]["working"]
        assert len(working.get("recent_files", [])) == 1

        # 模拟切换
        agent._last_cwd = "/different/path"
        agent._detect_workspace_switch()
        assert agent.session["memory"]["working"]["recent_files"] == []

    def test_workspace_fingerprint_consistent(self, temp_workspace):
        """同 workspace 两次构建 fingerprint 相同。"""
        from agent_runtime.workspace import WorkspaceContext

        fp1 = WorkspaceContext.build(str(temp_workspace)).fingerprint()
        fp2 = WorkspaceContext.build(str(temp_workspace)).fingerprint()
        assert isinstance(fp1, str) and len(fp1) > 0
        assert fp1 == fp2
