"""动态 Agent 裁剪单测：simple issue 跳过 Retriever。"""

from src.repair_factory import _SIMPLE_ISSUE_TYPES, AgentProfile
from src.state import RepairPlan


class TestSimpleIssueTypes:
    def test_import_error_is_simple(self):
        assert "import_error" in _SIMPLE_ISSUE_TYPES

    def test_syntax_error_is_simple(self):
        assert "syntax_error" in _SIMPLE_ISSUE_TYPES

    def test_type_error_is_not_simple(self):
        assert "type_error" not in _SIMPLE_ISSUE_TYPES


class TestAgentProfile:
    def test_simple_issue_skips_retriever(self):
        profile = AgentProfile.for_issue_type("import_error")
        assert profile.with_retriever is False

    def test_syntax_error_skips_retriever(self):
        profile = AgentProfile.for_issue_type("syntax_error")
        assert profile.with_retriever is False

    def test_complex_issue_keeps_retriever(self):
        profile = AgentProfile.for_issue_type("type_error")
        assert profile.with_retriever is True

    def test_unknown_issue_keeps_retriever(self):
        profile = AgentProfile.for_issue_type("unknown")
        assert profile.with_retriever is True

    def test_empty_issue_keeps_retriever(self):
        profile = AgentProfile.for_issue_type("")
        assert profile.with_retriever is True


class TestPruneAgents:
    def test_prune_sets_retriever_none_for_import_error(self):
        """import_error 时 _prune_agents_for_issue 将 retriever 设为 None。"""
        from unittest.mock import MagicMock

        # mock orchestrator with retriever
        mock = MagicMock()
        mock.retriever = "fake_retriever"

        from src.repair.pipeline import RepairPipelineMixin

        RepairPipelineMixin._prune_agents_for_issue(mock, RepairPlan(issue_type="import_error"))
        assert mock.retriever is None

    def test_prune_keeps_retriever_for_type_error(self):
        """type_error 时保留 retriever。"""
        from unittest.mock import MagicMock

        mock = MagicMock()
        mock.retriever = "fake_retriever"

        from src.repair.pipeline import RepairPipelineMixin

        RepairPipelineMixin._prune_agents_for_issue(mock, RepairPlan(issue_type="type_error"))
        assert mock.retriever == "fake_retriever"

    def test_prune_marks_prompt_variant(self):
        """裁剪时在 prompt_variants 中标记 agent_pruning=skip_retriever。"""
        from unittest.mock import MagicMock

        mock = MagicMock()
        mock.retriever = "fake_retriever"
        plan = RepairPlan(issue_type="import_error")

        from src.repair.pipeline import RepairPipelineMixin

        RepairPipelineMixin._prune_agents_for_issue(mock, plan)
        assert "agent_pruning" in (plan.prompt_variants or {})
        assert plan.prompt_variants["agent_pruning"] == "skip_retriever"


class TestPipelineSkipsRetriever:
    def test_run_localize_and_retrieve_skips_when_none(self):
        """retriever=None 时 _run_localize_and_retrieve 走 _run_localizer_only。"""
        from unittest.mock import MagicMock, patch

        from src.repair.pipeline import RepairPipelineMixin
        from src.state import RepairState

        mixin = RepairPipelineMixin()
        mixin.retriever = None
        mixin.localizer = MagicMock()

        state = RepairState(issue_input="import error")
        state.repair_plan = RepairPlan(issue_type="import_error")

        with patch.object(
            mixin, "_run_localizer_only", return_value=([], None, {}, {})
        ) as mock_only:
            mixin._run_localize_and_retrieve(state)
            mock_only.assert_called_once()
