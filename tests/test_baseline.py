"""Single-Agent Baseline 单测。"""

from agent_runtime.providers.clients import FakeModelClient
from agent_runtime.workspace import WorkspaceContext
from src.eval.baseline import (
    SingleAgentOrchestrator,
    create_single_agent_baseline,
    make_single_agent_factory,
)


class TestSingleAgentBaseline:
    def test_agent_has_all_tools(self, temp_workspace):
        ws = WorkspaceContext.build(str(temp_workspace))
        client = FakeModelClient(["<final>ok</final>"])
        agent = create_single_agent_baseline(client, ws, cwd=str(temp_workspace))
        names = set(agent.tools.keys())
        for expected in (
            "ast_parse",
            "stack_parse",
            "search",
            "read_file",
            "write_file",
            "patch_file",
            "sandbox_build",
            "sandbox_test",
            "sandbox_verify",
            "git_blame",
            "git_diff",
            "find_test",
        ):
            assert expected in names, f"missing tool: {expected}"

    def test_orchestrator_applies_patch(self, temp_workspace):
        (temp_workspace / "calc.py").write_text(
            "def add(a, b):\n    return a - b\n",
            encoding="utf-8",
        )
        ws = WorkspaceContext.build(str(temp_workspace))
        client = FakeModelClient(
            [
                '<final>[{"file_path":"calc.py","original_lines":"    return a - b","patched_lines":"    return a + b"}]</final>',
            ]
        )
        agent = create_single_agent_baseline(client, ws, cwd=str(temp_workspace))
        orch = SingleAgentOrchestrator(agent, str(temp_workspace))
        state = orch.repair("TypeError in calc.py")
        assert state.candidate_patches
        assert state.status == "patched"
        assert "return a + b" in (temp_workspace / "calc.py").read_text()

    def test_factory_returns_orchestrator(self, temp_workspace):
        client = FakeModelClient(["<final>ok</final>"])
        factory = make_single_agent_factory(model_client=client)
        orch = factory(str(temp_workspace))
        assert isinstance(orch, SingleAgentOrchestrator)
        state = orch.repair("test issue")
        assert state.status == "failed"
        assert "baseline" in state.agent_errors

    def test_orchestrator_detects_tool_patches(self, temp_workspace, monkeypatch):
        (temp_workspace / "calc.py").write_text("x = 1\n", encoding="utf-8")
        ws = WorkspaceContext.build(str(temp_workspace))
        client = FakeModelClient(["<final>done</final>"])
        agent = create_single_agent_baseline(client, ws, cwd=str(temp_workspace))
        orch = SingleAgentOrchestrator(agent, str(temp_workspace))

        def ask_and_patch(prompt):
            (temp_workspace / "calc.py").write_text("x = 2\n", encoding="utf-8")
            return "<final>done</final>"

        monkeypatch.setattr(agent, "ask", ask_and_patch)
        state = orch.repair("fix calc")
        assert state.status == "patched"
        assert not state.agent_errors
