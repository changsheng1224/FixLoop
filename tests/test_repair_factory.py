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
    def test_disabled_auto_never_probes_container(self, tmp_path, monkeypatch):
        from agent_runtime.providers.clients import FakeModelClient

        monkeypatch.setenv("FIXLOOP_SANDBOX_POLICY", "disabled")
        monkeypatch.setattr("src.repair_factory.try_create_verifier",
                            lambda *a, **k: pytest.fail("container probe"))
        owner = wire_orchestrator(FakeModelClient([]), str(tmp_path), execution_tier="auto")
        assert owner.verifier is None
        assert owner.execution_policy.initial_tier("python") == "host"

    def test_sandbox_factory_keeps_shared_root_and_source_project(self, tmp_path):
        from agent_runtime.linux_sandbox.task_context import SandboxTaskBinding
        from agent_runtime.providers.clients import FakeModelClient
        from agent_runtime.state_root import project_id

        source, copy, storage = (tmp_path / name for name in ("source", "copy", "storage"))
        source.mkdir()
        copy.mkdir()
        orch = wire_orchestrator(
            FakeModelClient([]),
            str(copy),
            project_root=str(source),
            state_root=str(storage),
            execution_backend="wsl_bwrap",
            sandbox_environment_manager=FakeSandboxEnvironmentManager(),
            workspace_policy="provided",
            skip_verify=True,
            dry_run=True,
        )
        spec = orch._sandbox_task_spec
        assert spec.state_base == str(storage)
        binding = SandboxTaskBinding.create(copy, copy, "run", spec)
        assert binding.project_id == project_id(source)
        assert (
            Path(binding.state_root) == storage / "projects" / project_id(source) / "tasks" / "run"
        )

    def test_external_state_and_exact_workspace_reach_patcher_and_verifier(
        self, tmp_path, monkeypatch
    ):
        from agent_runtime.providers.clients import FakeModelClient
        from agent_runtime.workspace import WorkspaceContext
        from src.repair.verification.execution_policy import VerificationBackend

        repo = tmp_path / "repo"
        repo.mkdir()
        state = tmp_path / "state"
        ws = WorkspaceContext(
            cwd=str(repo), repo_root=str(repo), doc_contents={}, dirty_file_hashes={}
        )
        monkeypatch.setattr(
            "src.repair_factory.WorkspaceContext.build", lambda *_: pytest.fail("rescanned root")
        )

        def verifier(client, workspace, root, **kw):
            assert client is None and workspace is None
            assert kw == {"required": True}
            return VerificationBackend()

        monkeypatch.setattr("src.repair_factory.try_create_verifier", verifier)
        orch = wire_orchestrator(
            FakeModelClient(["<final>ok</final>"]),
            str(repo),
            workspace_context=ws,
            state_root=str(state),
            execution_tier="container",
            runtime_overrides={"provider": "fake", "model": "fake-test", "max_new_tokens": 1234},
        )
        for agent in orch._active_agents:
            assert agent.workspace is ws
            assert agent.tool_context.root == str(repo)
            assert agent.state_root == str(state)
            assert agent.config.provider == "fake" and agent.config.model == "fake-test"
            assert agent.config.max_new_tokens == 1234

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
