"""No-Retriever Orchestrator 变体单测。"""

from agent_runtime.providers.clients import FakeModelClient
from agent_runtime.workspace import WorkspaceContext
from src.agents.localizer import create_localizer
from src.agents.patcher import create_patcher
from src.eval.variants import NoRetrieverOrchestrator, make_no_retriever_factory


class TestNoRetrieverOrchestrator:
    def test_skips_retriever_in_pipeline(self, temp_workspace):
        (temp_workspace / "calc.py").write_text(
            "def add(a, b):\n    return a - b\n",
            encoding="utf-8",
        )
        ws = WorkspaceContext.build(str(temp_workspace))
        loc = FakeModelClient(
            [
                '<final>[{"file_path":"calc.py","start_line":1,"end_line":2,"confidence":0.9}]</final>',
            ]
        )
        pat = FakeModelClient(
            [
                '<final>[{"file_path":"calc.py","original_lines":"    return a - b","patched_lines":"    return a + b"}]</final>',
            ]
        )
        orch = NoRetrieverOrchestrator(
            create_localizer(loc, ws, cwd=str(temp_workspace)),
            None,
            create_patcher(pat, ws, cwd=str(temp_workspace)),
        )
        state = orch.repair("TypeError at calc.py:2", max_retries=1)
        assert state.node_timings.get("retriever_ms", 0) == 0
        assert state.status == "patched"
        assert "return a + b" in (temp_workspace / "calc.py").read_text()

    def test_factory_returns_no_retriever_orchestrator(self, temp_workspace):
        client = FakeModelClient(["<final>ok</final>"])
        factory = make_no_retriever_factory(skip_verify=True, model_client=client)
        orch = factory(str(temp_workspace))
        assert isinstance(orch, NoRetrieverOrchestrator)
        assert orch.retriever is None
