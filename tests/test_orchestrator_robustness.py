"""Orchestrator 鲁棒性测试：Agent 失败降级、沙箱清理。"""

from unittest.mock import MagicMock, patch

import pytest

from agent_runtime.providers.clients import FakeModelClient
from agent_runtime.tool_context import ToolContext
from agent_runtime.workspace import WorkspaceContext
from src.agents.localizer import create_localizer
from src.agents.patcher import create_patcher
from src.agents.retriever import create_retriever
from src.harness.sandbox_manager import Sandbox
from src.orchestrator import Orchestrator
from src.tools.sandbox_tools import _run_test_in_sandbox


class TestOrchestratorRobustness:
    def test_localizer_failure_uses_plan_fallback(self, temp_workspace):
        """Localizer 抛错时，从 RepairPlan 降级生成 suspect。"""
        (temp_workspace / "calc.py").write_text(
            "def add(a, b):\n    return a + b\n",
            encoding="utf-8",
        )
        ws = WorkspaceContext.build(str(temp_workspace))
        loc = create_localizer(FakeModelClient([]), ws)
        ret = create_retriever(
            FakeModelClient(['<final>{"related_tests":[]}</final>']),
            ws,
        )
        pat = create_patcher(
            FakeModelClient(["<final>[]</final>"]),
            ws,
        )
        orch = Orchestrator(loc, ret, pat)
        state = orch.repair("TypeError at calc.py:42")
        assert "localizer" in state.agent_errors
        assert len(state.suspect_locations) == 1
        assert state.suspect_locations[0].file_path == "calc.py"
        assert state.suspect_locations[0].reason == "RepairPlan 降级定位"

    def test_retriever_failure_still_patches(self, temp_workspace):
        """Retriever 失败时流水线仍可生成补丁。"""
        (temp_workspace / "calc.py").write_text(
            "def add(a, b):\n    return a + b\n",
            encoding="utf-8",
        )
        ws = WorkspaceContext.build(str(temp_workspace))
        loc = create_localizer(
            FakeModelClient(
                [
                    '<final>[{"file_path":"calc.py","start_line":1,"end_line":1,'
                    '"reason":"堆栈","confidence":0.9}]</final>',
                ]
            ),
            ws,
        )
        ret = create_retriever(FakeModelClient([]), ws)
        pat = create_patcher(
            FakeModelClient(
                [
                    '<final>[{"file_path":"calc.py","original_lines":"return a + b",'
                    '"patched_lines":"return int(a) + int(b)","explanation":"fix"}]</final>',
                ]
            ),
            ws,
        )
        orch = Orchestrator(loc, ret, pat)
        state = orch.repair("TypeError at calc.py:1")
        assert "retriever" in state.agent_errors
        assert state.retrieved_context is not None
        assert len(state.candidate_patches) == 1
        assert state.status == "fixed"
        assert state.node_timings.get("verify_skipped") is True

    def test_sandbox_destroy_on_exception(self):
        """pytest 异常时仍销毁沙箱容器。"""
        mgr = MagicMock()
        sandbox = Sandbox(id="sb-destroy-me", profile="python")
        sandbox.timings = {}
        mgr.create.return_value = sandbox

        ctx = ToolContext(root=".")
        with patch("src.harness.sandbox_verify.SandboxManager", return_value=mgr):
            with patch("src.harness.sandbox_verify.assert_sandbox_available", lambda: None):
                with patch(
                    "src.harness.sandbox_verify.maybe_pip_install",
                    return_value=("skipped", 0, None),
                ):
                    with patch("src.harness.sandbox_verify.PythonTestRunner") as runner_cls:
                        runner_cls.return_value.run.side_effect = RuntimeError("pytest boom")
                        with pytest.raises(RuntimeError, match="pytest boom"):
                            _run_test_in_sandbox(ctx, ".", "")
        mgr.destroy.assert_called_once_with(sandbox)
        assert getattr(ctx, "_sandbox_id", "x") is None
