"""Gate 3 argument governance: declared path args and argv-supplied args.

A tool's path-bearing argument is not always called ``path`` (``git_blame.file``,
``find_test.file_path``, ``sandbox_verify.repo_path``), and an argument that is
concatenated into a subprocess argv can be read by that program as an option.
Both used to slip past Gate 3, which only inspected the literal key ``path``.
"""

import pytest

from agent_runtime.config import AgentConfig
from agent_runtime.providers.clients import FakeModelClient
from agent_runtime.runtime import Agent
from agent_runtime.tool_context import ToolContext
from agent_runtime.tool_executor import ToolExecutor
from agent_runtime.tool_spec import bind_execution_tools
from agent_runtime.tools import build_tool_registry
from agent_runtime.workspace import WorkspaceContext
from src.tools.registry import build_repair_tools
from src.tools.sandbox_tools import build_sandbox_tool_registry
from src.tools.spec import default_repair_tool_registry
from tests.repair_support import build_repository


@pytest.fixture
def repair_repo(tmp_path):
    repo = build_repository(
        tmp_path / "repo",
        {
            "README.md": "# repo\n",
            "pyproject.toml": "[project]\nname='repo'\n",
            "pkg/mod.py": "def helper():\n    return 1\n",
        },
        git=True,
    )
    return repo


@pytest.fixture
def executor(repair_repo):
    """Executor over the repair canonical registry, so L2 tools are reachable."""
    context = ToolContext(str(repair_repo))
    tools = build_tool_registry(context)
    tools.update(build_repair_tools(context))
    tools.update(build_sandbox_tool_registry(context))
    tools = bind_execution_tools(tools, default_repair_tool_registry(sandbox_mode=False))
    agent = Agent(
        config=AgentConfig(provider="fake", approval="auto"),
        model_client=FakeModelClient(["<final>ok</final>"]),
        workspace=WorkspaceContext.build(str(repair_repo)),
        tools=tools,
    )
    return ToolExecutor(agent=agent, approval_policy="auto")


class TestArgvSuppliedArgs:
    """Values concatenated into an argv must not be readable as options."""

    @pytest.mark.parametrize(
        "commit_a",
        [
            "--output=/tmp/pwned",
            "--output=./pwned",
            "-O/tmp/pwned",
            "HEAD\n--output=x",
            "HEAD\r--output=x",
            "HEAD\x00--output=x",
        ],
    )
    def test_git_diff_rejects_option_like_revision(self, executor, commit_a):
        result = executor.execute_gated("git_diff", {"commit_a": commit_a, "commit_b": "HEAD"})
        assert result.status == "rejected"
        assert result.error_code == "option_injection"

    def test_git_diff_still_accepts_a_plain_revision_range(self, executor):
        result = executor.execute_gated("git_diff", {"commit_a": "HEAD", "commit_b": "HEAD"})
        assert result.error_code != "option_injection"

    @pytest.mark.parametrize(
        "nodeid",
        ["--rootdir=/", "--confcutdir=/", "-p", "-k"],
    )
    def test_quick_test_rejects_option_like_nodeid(self, executor, nodeid):
        result = executor.execute_gated("quick_test", {"nodeid": nodeid})
        assert result.status == "rejected"
        assert result.error_code == "option_injection"

    def test_quick_test_rejects_a_target_outside_the_workspace(self, executor):
        """The host tier now applies the same containment check as the sandbox."""
        result = executor.execute_gated("quick_test", {"nodeid": "../outside.py"})
        assert result.status != "success"
        assert result.error_code in {"invalid_args", "path_escape"}


class TestDeclaredPathArgs:
    """Every declared path argument is resolved and sensitivity-checked."""

    @pytest.mark.parametrize(
        ("tool", "key"),
        [
            ("git_blame", "file"),
            ("find_test", "file_path"),
            ("sandbox_build", "repo_path"),
            ("sandbox_test", "test_path"),
            ("sandbox_verify", "repo_path"),
        ],
    )
    def test_path_argument_escapes_are_rejected(self, executor, tool, key):
        result = executor.execute_gated(tool, {key: "../../outside.txt"})
        assert result.status == "rejected"
        assert result.error_code in {"path_escape", "invalid_args"}

    def test_declarations_reach_the_execution_registry(self, executor):
        """The ToolSpec declaration must survive the projection into agent.tools."""
        declared = executor.agent.tools
        assert declared["git_diff"]["argv_args"] == ["commit_a", "commit_b"]
        assert declared["quick_test"]["argv_args"] == ["nodeid"]
        assert declared["git_blame"]["path_args"] == ["file"]
        assert declared["find_test"]["path_args"] == ["file_path"]
        assert set(declared["sandbox_verify"]["path_args"]) == {"repo_path", "test_path"}


class TestUndeclaredToolsKeepWordDefault:
    def test_tools_without_a_declaration_still_check_path(self, executor):
        assert executor.agent.tools["read_file"]["path_args"] == ["path"]

    def test_relative_path_escape_is_still_rejected(self, executor):
        result = executor.execute_gated("read_file", {"path": "../../etc/hosts"})
        assert result.status == "rejected"
