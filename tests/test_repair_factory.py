"""Patcher-primary factory wiring."""

from __future__ import annotations

import tempfile

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
