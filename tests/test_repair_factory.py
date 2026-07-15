"""AgentProfile 动态裁剪单测（V1.4-Bonus15d）。"""

from __future__ import annotations

import tempfile

from src.repair_factory import AgentProfile, wire_orchestrator


class TestAgentProfile:
    def test_import_error_skips_retriever(self):
        profile = AgentProfile.for_issue_type("import_error")
        assert profile.with_retriever is False

    def test_syntax_error_skips_retriever(self):
        profile = AgentProfile.for_issue_type("syntax_error")
        assert profile.with_retriever is False

    def test_composite_keeps_retriever(self):
        profile = AgentProfile.for_issue_type("composite")
        assert profile.with_retriever is True

    def test_type_error_keeps_retriever(self):
        profile = AgentProfile.for_issue_type("type_error")
        assert profile.with_retriever is True

    def test_unknown_type_defaults_to_retriever(self):
        profile = AgentProfile.for_issue_type("unknown")
        assert profile.with_retriever is True


class TestWireWithProfile:
    def test_profile_skips_retriever(self):
        with tempfile.TemporaryDirectory() as tmp:
            from agent_runtime.providers.clients import FakeModelClient

            profile = AgentProfile.for_issue_type("import_error")
            orch = wire_orchestrator(
                FakeModelClient(outputs=["<final>ok</final>"]),
                str(tmp),
                agent_profile=profile,
                skip_verify=True,
                dry_run=True,
            )
            assert orch.localizer is not None
            assert orch.retriever is None  # 被裁剪
            assert orch.patcher is not None

    def test_full_profile_creates_retriever(self):
        with tempfile.TemporaryDirectory() as tmp:
            from agent_runtime.providers.clients import FakeModelClient

            profile = AgentProfile.for_issue_type("type_error")
            orch = wire_orchestrator(
                FakeModelClient(outputs=["<final>ok</final>"]),
                str(tmp),
                agent_profile=profile,
                skip_verify=True,
                dry_run=True,
            )
            assert orch.retriever is not None

    def test_explicit_with_retriever_overrides_profile(self):
        with tempfile.TemporaryDirectory() as tmp:
            from agent_runtime.providers.clients import FakeModelClient

            # profile 说跳过，但显式 with_retriever=True 应覆盖
            profile = AgentProfile.for_issue_type("import_error")
            orch = wire_orchestrator(
                FakeModelClient(outputs=["<final>ok</final>"]),
                str(tmp),
                with_retriever=True,
                agent_profile=profile,
                skip_verify=True,
                dry_run=True,
            )
            assert orch.retriever is not None
