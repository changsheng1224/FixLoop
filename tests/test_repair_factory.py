"""Patcher-primary factory wiring."""

from __future__ import annotations

import tempfile
from pathlib import Path

from src.repair_factory import wire_orchestrator


class TestRuntimeWiring:
    def test_wires_patcher_without_verifier(self):
        with tempfile.TemporaryDirectory() as tmp:
            from agent_runtime.providers.clients import FakeModelClient

            orch = wire_orchestrator(
                FakeModelClient(outputs=["<final>ok</final>"]),
                str(tmp),
                skip_verify=True,
                dry_run=True,
            )
            assert orch.patcher is not None
            assert orch.verifier is None
            assert orch._repair_gateways

    def test_code_lookup_uses_agent_context_and_trusted_mode(self):
        from agent_runtime.providers.clients import FakeModelClient

        with tempfile.TemporaryDirectory() as tmp:
            Path(tmp, "a.py").write_text("def target():\n    return 1\n", encoding="utf-8")
            orch = wire_orchestrator(
                FakeModelClient(outputs=["<final>ok</final>"]),
                tmp,
                skip_verify=True,
                dry_run=False,
                code_exploration_mode="lsp",
                code_exploration_server_argv=(),
            )
            result = orch.patcher.execute_tool(
                "code_lookup", {"path": "a.py", "line": 1, "column": 5}
            )
            assert orch.patcher.tool_context.exploration_mode == "lsp"
            assert result.metadata["retrieval_result"]["degradation_reason"]
