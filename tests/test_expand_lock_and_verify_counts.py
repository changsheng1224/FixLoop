"""Two independent regressions: expand_lock's declared side effect, and how
host-tier pytest counts are read out of the verifier's combined output.
"""

import pytest

from agent_runtime.config import AgentConfig
from agent_runtime.providers.clients import FakeModelClient
from agent_runtime.runtime import Agent
from agent_runtime.tool_context import ToolContext
from agent_runtime.tool_executor import ToolExecutor
from agent_runtime.workspace import WorkspaceContext
from src.repair.execution.edit_lock import EditLockState
from src.tools.composite import build_repair_canonical_tools
from tests.repair_support import build_repository

FILES = {
    "README.md": "# repo\n",
    "pyproject.toml": "[project]\nname='repo'\n",
    "pkg/mod.py": "def helper():\n    return 1\n",
}


@pytest.fixture
def governance():
    from src.collaboration_governance import CollaborationGovernance
    from src.tools.spec import default_repair_tool_registry

    return CollaborationGovernance(registry=default_repair_tool_registry(sandbox_mode=False))


@pytest.fixture
def repairs(tmp_path):
    """(tool context, executor) over the repair canonical registry."""
    repo = build_repository(tmp_path / "repo", FILES, git=True)
    context = ToolContext(str(repo))
    context.edit_lock = EditLockState(repo_root=repo)
    tools = build_repair_canonical_tools(context)
    agent = Agent(
        config=AgentConfig(provider="fake", approval="auto"),
        model_client=FakeModelClient(["<final>ok</final>"]),
        workspace=WorkspaceContext.build(str(repo)),
        tools=tools,
    )
    return context, ToolExecutor(agent=agent, approval_policy="auto")


class TestExpandLockSideEffect:
    """expand_lock mutates write authority, not files."""

    def test_declares_a_non_read_side_effect(self, repairs):
        _, executor = repairs
        assert executor.agent.tools["expand_lock"]["side_effect"] == "state"

    def test_read_only_mode_blocks_it(self, governance):
        """The defect: with side_effect "read" this returned allowed."""
        decision = governance.authorize(
            "expand_lock", role="patcher", phase="patch", control_mode="read_only"
        )
        assert not decision.allowed
        assert decision.reason == "human_read_only"

    def test_write_tools_are_blocked_the_same_way(self, governance):
        decision = governance.authorize(
            "patch_file", role="patcher", phase="patch", control_mode="read_only"
        )
        assert not decision.allowed

    def test_ordinary_mode_still_allows_it(self, governance):
        assert governance.authorize("expand_lock", role="patcher", phase="patch").allowed

    def test_successful_expansion_is_reported_verbatim(self, repairs):
        """The grant must reach the caller, not be rewritten as a write result."""
        context, executor = repairs
        result = executor.execute_gated(
            "expand_lock", {"path": "pkg/new_mod.py", "operation": "create"}
        )
        assert result.status == "success"
        assert result.error_code in ("", None)
        assert "expand_lock ok" in str(result.content)
        assert context.edit_lock.allowed_create == {"pkg/new_mod.py"}

    def test_it_is_blocked_while_a_prior_side_effect_is_unconfirmed(self, repairs):
        _, executor = repairs
        executor.agent.tool_context.execution_uncertain = True
        result = executor.execute_gated(
            "expand_lock", {"path": "pkg/new_mod.py", "operation": "create"}
        )
        assert result.status == "rejected"
        assert result.error_code == "execution_uncertain"


class TestHostVerifyCounts:
    """pytest counts must survive stderr appended after the summary line."""

    def test_counts_survive_a_long_stderr_tail(self):
        from src.repair.verification.verify import _pytest_counts

        stdout = "....\n===== 3 passed, 1 skipped in 0.12s =====\n"
        stderr = "plugin warning: deprecated hook\n" * 40
        assert len(stderr) > 1000

        counts = _pytest_counts(stdout + stderr)
        assert counts == {"passed": 3, "failed": 0, "error": 0, "skipped": 1}

    def test_a_short_stderr_tail_still_parses(self):
        from src.repair.verification.verify import _pytest_counts

        counts = _pytest_counts("===== 3 passed in 0.1s =====\nwarn\n")
        assert counts["passed"] == 3

    def test_failure_counts_are_read(self):
        from src.repair.verification.verify import _pytest_counts

        counts = _pytest_counts("===== 2 failed, 1 passed in 0.2s =====\n")
        assert counts["failed"] == 2
        assert counts["passed"] == 1

    def test_summary_lines_are_not_summed_together(self):
        from src.repair.verification.verify import _pytest_counts

        counts = _pytest_counts("1 passed in 0.1s\n===== 3 passed in 0.2s =====\n")
        assert counts["passed"] == 3

    def test_output_without_a_summary_yields_zero(self):
        from src.repair.verification.verify import _pytest_counts

        assert sum(_pytest_counts("nothing ran here\n").values()) == 0
