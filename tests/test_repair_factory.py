"""Patcher-primary factory wiring."""

from __future__ import annotations

import tempfile
from pathlib import Path

import pytest

from src.repair_factory import wire_orchestrator


def _prepared_environment():
    from agent_runtime.linux_sandbox.environment import PreparedEnvironment
    from agent_runtime.linux_sandbox.task_context import SandboxEnvironmentIdentity

    identity = SandboxEnvironmentIdentity(
        distribution="FixLoop-Test",
        distribution_version="26.04",
        runtime_version="test-runtime",
        runtime_digest="a" * 64,
    )
    return PreparedEnvironment(identity, "b" * 64, "C:/FixLoop/Test", {"ready": True})


class FakeSandboxEnvironmentManager:
    def __init__(self):
        self.calls = []

    def ensure(self, *, package_proxy=""):
        self.calls.append(package_proxy)
        return _prepared_environment()


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

    def test_wsl_bwrap_binds_shared_context_and_sandbox_gateway(self, monkeypatch):
        from agent_runtime.providers.clients import FakeModelClient

        with tempfile.TemporaryDirectory() as tmp, tempfile.TemporaryDirectory() as state_home:
            monkeypatch.setenv("LOCALAPPDATA", state_home)
            monkeypatch.setattr(
                "src.repair_factory.WorkspaceContext.build",
                lambda *_: pytest.fail("sandbox initialization scanned the host workspace"),
            )
            manager = FakeSandboxEnvironmentManager()
            orch = wire_orchestrator(
                FakeModelClient(outputs=["<final>ok</final>"]),
                tmp,
                execution_backend="wsl_bwrap",
                sandbox_environment_manager=manager,
                sandbox_package_proxy="http://proxy.test:8080",
                skip_verify=True,
                dry_run=True,
            )
            assert manager.calls == ["http://proxy.test:8080"]
            assert orch._sandbox_context is orch.patcher.tool_context
            assert orch._sandbox_context.sandbox_mode is True
            assert orch._sandbox_task_spec.environment.distribution == "FixLoop-Test"
            assert orch._sandbox_backend_factory is not None
            assert orch.verifier is None
            assert orch.patcher._repair_gateway._registry.get("run_shell").lifecycle == "active"
            assert orch.patcher._repair_gateway._registry.get("run_shell").roles == frozenset(
                {"patcher"}
            )

    def test_wsl_bwrap_environment_failure_is_required_and_fails_closed(self, tmp_path):
        from src.repair_factory import RequiredVerifierError

        class FailingManager:
            def ensure(self, **kwargs):
                raise RuntimeError("no WSL")

        with pytest.raises(RequiredVerifierError, match="sandbox unavailable"):
            wire_orchestrator(
                None,
                str(tmp_path),
                execution_backend="wsl_bwrap",
                sandbox_environment_manager=FailingManager(),
            )
