"""官方 GitHub MCP stdio 真连：Transport / 映射 / 假进程。"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

from agent_runtime.mcp.official import OfficialMappedClient, build_official_stdio_command
from agent_runtime.mcp.official_map import adapt_local_call
from agent_runtime.mcp.registry import build_github_mcp_tool_registry, open_github_mcp_client
from agent_runtime.mcp.stdio import StdioTransport

FAKE_SERVER = Path(__file__).resolve().parent / "fixtures" / "fake_github_mcp_server.py"


@pytest.fixture
def fake_stdio_client():
    cmd = [sys.executable, str(FAKE_SERVER)]
    transport = StdioTransport(cmd, timeout_s=10.0)
    transport.start()
    client = OfficialMappedClient(transport, timeout_s=10.0)
    try:
        yield client
    finally:
        client.close()


class TestOfficialArgAdapt:
    def test_get_issue_maps_method(self):
        remote, args = adapt_local_call(
            "github_get_issue",
            {"owner": "o", "repo": "r", "number": 7},
        )
        assert remote == "issue_read"
        assert args == {
            "owner": "o",
            "repo": "r",
            "issue_number": 7,
            "method": "get",
        }

    def test_draft_pr_forces_draft(self):
        remote, args = adapt_local_call(
            "github_create_draft_pr",
            {
                "owner": "o",
                "repo": "r",
                "title": "t",
                "head": "feat",
                "base": "main",
            },
        )
        assert remote == "create_pull_request"
        assert args["draft"] is True
        assert args["base"] == "main"


class TestStdioFakeServer:
    def test_handshake_list_and_call(self, fake_stdio_client):
        specs = fake_stdio_client.list_tools()
        names = {s.name for s in specs}
        assert "github_list_issues" in names
        assert "github_create_draft_pr" in names
        assert "github_merge_pull_request" not in names
        assert "merge_pull_request" not in names

        result = fake_stdio_client.call_tool(
            "github_list_issues",
            {"owner": "acme", "repo": "demo"},
        )
        assert "bug" in result.observation()

    def test_mapped_draft_pr(self, fake_stdio_client):
        result = fake_stdio_client.call_tool(
            "github_create_draft_pr",
            {
                "owner": "acme",
                "repo": "demo",
                "title": "Fix",
                "head": "fix/x",
            },
        )
        obs = result.observation()
        assert "Fix" in obs
        assert "true" in obs.lower() or "True" in obs

    def test_registry_from_official_mapped(self, fake_stdio_client):
        tools = build_github_mcp_tool_registry(fake_stdio_client)
        assert "github_get_issue" in tools
        out = tools["github_get_issue"]["run"](
            {"owner": "acme", "repo": "demo", "number": 1}
        )
        assert "get" in out
        assert "merge_pull_request" not in tools


class TestOpenClientMode:
    def test_force_mock_even_with_token(self, monkeypatch):
        monkeypatch.setenv("FIXLOOP_GITHUB_MCP_MODE", "mock")
        monkeypatch.setenv("GITHUB_PERSONAL_ACCESS_TOKEN", "ghp_fake_for_test")
        client, handle = open_github_mcp_client()
        assert client.server_name == "mock-github-mcp"
        assert "github_list_issues" in {s.name for s in client.list_tools()}

    def test_command_builder_docker_default(self, monkeypatch):
        monkeypatch.delenv("FIXLOOP_GITHUB_MCP_COMMAND", raising=False)
        monkeypatch.setenv("FIXLOOP_GITHUB_MCP_IMAGE", "ghcr.io/example/mcp:test")
        cmd, env = build_official_stdio_command(token="tok")
        assert cmd[0] == "docker"
        assert "ghcr.io/example/mcp:test" in cmd
        assert env["GITHUB_PERSONAL_ACCESS_TOKEN"] == "tok"

    def test_command_builder_custom(self, monkeypatch):
        monkeypatch.setenv(
            "FIXLOOP_GITHUB_MCP_COMMAND",
            f"{sys.executable} {FAKE_SERVER}",
        )
        cmd, env = build_official_stdio_command(token="tok")
        assert cmd[0] == sys.executable
        assert env["GITHUB_PERSONAL_ACCESS_TOKEN"] == "tok"


@pytest.mark.skipif(
    not os.environ.get("FIXLOOP_GITHUB_MCP_LIVE"),
    reason="set FIXLOOP_GITHUB_MCP_LIVE=1 and PAT to run live official MCP",
)
class TestLiveOfficialGithubMcp:
    """真连官方 server：只读工具。需 Docker/二进制 + PAT。

    环境变量：
    - GITHUB_PERSONAL_ACCESS_TOKEN / GITHUB_PAT / FIXLOOP_GITHUB_TOKEN
    - FIXLOOP_GITHUB_MCP_OWNER / FIXLOOP_GITHUB_MCP_REPO（默认 changsheng1224/FixLoop）
    - FIXLOOP_GITHUB_MCP_COMMAND（可选，覆盖 docker 默认）
    """

    @pytest.fixture(scope="class")
    def live_client(self):
        from agent_runtime.mcp.official import (
            build_official_github_mcp_client,
            resolve_github_token,
        )

        if not resolve_github_token():
            pytest.skip("no GitHub PAT in environment")
        client = build_official_github_mcp_client(timeout_s=180.0)
        try:
            yield client
        finally:
            client.close()

    @pytest.fixture(scope="class")
    def repo(self):
        return (
            os.environ.get("FIXLOOP_GITHUB_MCP_OWNER", "changsheng1224"),
            os.environ.get("FIXLOOP_GITHUB_MCP_REPO", "FixLoop"),
        )

    @staticmethod
    def _call_ok(client, tool: str, args: dict, *, retries: int = 3) -> str:
        import time

        last = ""
        for attempt in range(retries):
            last = client.call_tool(tool, args).observation()
            if not last.startswith("Error:"):
                return last
            transient = any(
                s in last.lower()
                for s in ("timeout", "tls", "connection reset", "temporarily", "eof")
            )
            if not transient or attempt + 1 >= retries:
                break
            time.sleep(1.5 * (attempt + 1))
        assert not last.startswith("Error:"), last[:800]
        return last

    def test_list_tools_exposes_local_names(self, live_client):
        names = {s.name for s in live_client.list_tools()}
        for expected in (
            "github_list_issues",
            "github_get_repo",
            "github_list_branches",
            "github_list_commits",
            "github_list_pull_requests",
            "github_list_workflow_runs",
            "github_create_draft_pr",
        ):
            assert expected in names, f"missing {expected}; got={sorted(names)}"
        assert "merge_pull_request" not in names

    def test_list_issues(self, live_client, repo):
        owner, name = repo
        self._call_ok(
            live_client,
            "github_list_issues",
            {"owner": owner, "repo": name, "limit": 3},
        )

    def test_get_repo_root_listing(self, live_client, repo):
        owner, name = repo
        self._call_ok(live_client, "github_get_repo", {"owner": owner, "repo": name})

    def test_list_branches(self, live_client, repo):
        owner, name = repo
        obs = self._call_ok(
            live_client,
            "github_list_branches",
            {"owner": owner, "repo": name, "limit": 30},
        )
        assert '"name"' in obs
        assert '"sha"' in obs

    def test_list_commits(self, live_client, repo):
        owner, name = repo
        self._call_ok(
            live_client,
            "github_list_commits",
            {"owner": owner, "repo": name, "limit": 3},
        )

    def test_list_pull_requests(self, live_client, repo):
        owner, name = repo
        self._call_ok(
            live_client,
            "github_list_pull_requests",
            {"owner": owner, "repo": name, "state": "all"},
        )

    def test_list_workflow_runs(self, live_client, repo):
        owner, name = repo
        self._call_ok(
            live_client,
            "github_list_workflow_runs",
            {"owner": owner, "repo": name, "limit": 3},
        )

    def test_get_commit_head(self, live_client, repo):
        owner, name = repo
        listed = self._call_ok(
            live_client,
            "github_list_commits",
            {"owner": owner, "repo": name, "limit": 1},
        )
        import json
        import re

        sha = None
        try:
            data = json.loads(listed)
            if isinstance(data, list) and data:
                sha = data[0].get("sha") or data[0].get("oid")
            elif isinstance(data, dict):
                sha = data.get("sha") or data.get("oid")
        except json.JSONDecodeError:
            m = re.search(r'"sha"\s*:\s*"([0-9a-f]{7,40})"', listed, re.I)
            sha = m.group(1) if m else None
        if not sha:
            pytest.skip("could not parse commit sha")
        self._call_ok(
            live_client,
            "github_get_commit",
            {"owner": owner, "repo": name, "sha": sha},
        )

    def test_get_pull_request_when_present(self, live_client, repo):
        owner, name = repo
        listed = self._call_ok(
            live_client,
            "github_list_pull_requests",
            {"owner": owner, "repo": name, "state": "all"},
        )
        import json
        import re

        number = os.environ.get("FIXLOOP_GITHUB_MCP_PR")
        if not number:
            try:
                data = json.loads(listed)
                if isinstance(data, list) and data:
                    number = str(data[0].get("number") or data[0].get("pullNumber"))
            except json.JSONDecodeError:
                m = re.search(r'"number"\s*:\s*(\d+)', listed)
                number = m.group(1) if m else None
        if not number:
            pytest.skip("no pull request to fetch")
        self._call_ok(
            live_client,
            "github_get_pull_request",
            {"owner": owner, "repo": name, "number": int(number)},
        )

    def test_get_issue_when_present(self, live_client, repo):
        owner, name = repo
        listed = self._call_ok(
            live_client,
            "github_list_issues",
            {"owner": owner, "repo": name, "state": "all", "limit": 1},
        )
        if listed.strip() in ("[]", "", "null"):
            pytest.skip("repo has no issues to fetch")
        number = os.environ.get("FIXLOOP_GITHUB_MCP_ISSUE")
        if not number:
            import json
            import re

            try:
                data = json.loads(listed)
                if isinstance(data, list) and data:
                    number = str(data[0].get("number") or data[0].get("issue_number"))
                elif isinstance(data, dict) and "number" in data:
                    number = str(data["number"])
            except json.JSONDecodeError:
                m = re.search(r'"number"\s*:\s*(\d+)', listed)
                number = m.group(1) if m else None
        if not number:
            pytest.skip("could not determine issue number from list_issues")
        self._call_ok(
            live_client,
            "github_get_issue",
            {"owner": owner, "repo": name, "number": int(number)},
        )
