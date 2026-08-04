"""进程内 Mock GitHub MCP Server（tools/list + tools/call）。"""

from __future__ import annotations

import json
from typing import Any

from agent_runtime.mcp.github_allowlist import GITHUB_MCP_DENIED_TOOLS


def _text(payload: Any) -> dict[str, Any]:
    if isinstance(payload, str):
        body = payload
    else:
        body = json.dumps(payload, ensure_ascii=False, indent=2)
    return {"content": [{"type": "text", "text": body}], "isError": False}


def _tool(name: str, description: str, properties: dict, required: list[str]) -> dict:
    return {
        "name": name,
        "description": description,
        "inputSchema": {
            "type": "object",
            "properties": properties,
            "required": required,
        },
    }


class MockGitHubMcpServer:
    """假 GitHub 工具集：含只读工具 + draft_pr + 若干危险工具（供拒绝测试）。"""

    def __init__(self, *, fail_mode: str | None = None, call_delay_s: float = 0.0) -> None:
        """
        fail_mode:
          - None: 正常
          - "unavailable": 所有请求失败
          - "slow": 配合 client.timeout 测超时（由 call_delay_s 控制）
        """
        self.fail_mode = fail_mode
        self.call_delay_s = call_delay_s
        self.call_log: list[tuple[str, dict[str, Any]]] = []
        self._draft_prs: list[dict[str, Any]] = []

    def handle(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        if self.fail_mode == "unavailable":
            return {"error": "mock server unavailable"}
        if method == "tools/list":
            return {"tools": self._all_tool_defs()}
        if method == "tools/call":
            if self.call_delay_s > 0:
                import time

                time.sleep(self.call_delay_s)
            name = str(params.get("name") or "")
            args = dict(params.get("arguments") or {})
            self.call_log.append((name, args))
            return self._call(name, args)
        return {"error": f"unknown method {method}"}

    def _all_tool_defs(self) -> list[dict]:
        owner_repo = {
            "owner": {"type": "string", "description": "repo owner"},
            "repo": {"type": "string", "description": "repo name"},
        }
        tools = [
            _tool(
                "github_list_issues",
                "List issues in a repository",
                {
                    **owner_repo,
                    "state": {"type": "string", "default": "open"},
                    "limit": {"type": "integer", "default": 20},
                },
                ["owner", "repo"],
            ),
            _tool(
                "github_get_issue",
                "Get a single issue",
                {**owner_repo, "number": {"type": "integer"}},
                ["owner", "repo", "number"],
            ),
            _tool(
                "github_list_issue_comments",
                "List comments on an issue",
                {**owner_repo, "number": {"type": "integer"}},
                ["owner", "repo", "number"],
            ),
            _tool(
                "github_get_repo",
                "Get repository metadata",
                owner_repo,
                ["owner", "repo"],
            ),
            _tool(
                "github_list_commits",
                "List commits",
                {
                    **owner_repo,
                    "sha": {"type": "string", "default": ""},
                    "limit": {"type": "integer", "default": 10},
                },
                ["owner", "repo"],
            ),
            _tool(
                "github_get_commit",
                "Get a commit by sha",
                {**owner_repo, "sha": {"type": "string"}},
                ["owner", "repo", "sha"],
            ),
            _tool(
                "github_list_branches",
                "List branches",
                {**owner_repo, "limit": {"type": "integer", "default": 20}},
                ["owner", "repo"],
            ),
            _tool(
                "github_list_pull_requests",
                "List pull requests",
                {**owner_repo, "state": {"type": "string", "default": "open"}},
                ["owner", "repo"],
            ),
            _tool(
                "github_get_pull_request",
                "Get a pull request",
                {**owner_repo, "number": {"type": "integer"}},
                ["owner", "repo", "number"],
            ),
            _tool(
                "github_list_workflow_runs",
                "List GitHub Actions workflow runs",
                {**owner_repo, "limit": {"type": "integer", "default": 10}},
                ["owner", "repo"],
            ),
            _tool(
                "github_create_draft_pr",
                "Create a draft pull request (write; requires human approval)",
                {
                    **owner_repo,
                    "title": {"type": "string"},
                    "head": {"type": "string"},
                    "base": {"type": "string", "default": "master"},
                    "body": {"type": "string", "default": ""},
                },
                ["owner", "repo", "title", "head"],
            ),
            # 危险工具：出现在 list，但 allowlist 拒绝注册
            _tool(
                "github_merge_pull_request",
                "Merge a pull request (FORBIDDEN)",
                {**owner_repo, "number": {"type": "integer"}},
                ["owner", "repo", "number"],
            ),
            _tool(
                "github_delete_branch",
                "Delete a branch (FORBIDDEN)",
                {**owner_repo, "branch": {"type": "string"}},
                ["owner", "repo", "branch"],
            ),
            _tool(
                "github_update_repo_secrets",
                "Update repository secrets (FORBIDDEN)",
                {**owner_repo, "name": {"type": "string"}, "value": {"type": "string"}},
                ["owner", "repo", "name", "value"],
            ),
        ]
        assert GITHUB_MCP_DENIED_TOOLS  # noqa: B018 — document coupling
        return tools

    def _call(self, name: str, args: dict[str, Any]) -> dict[str, Any]:
        owner = args.get("owner", "acme")
        repo = args.get("repo", "demo")
        if name == "github_list_issues":
            return _text(
                [
                    {"number": 1, "title": "TypeError in calc", "state": "open"},
                    {"number": 2, "title": "docs typo", "state": "open"},
                ]
            )
        if name == "github_get_issue":
            return _text(
                {
                    "number": args.get("number"),
                    "title": "TypeError in calc",
                    "body": "repro steps...",
                    "state": "open",
                    "html_url": f"https://github.com/{owner}/{repo}/issues/{args.get('number')}",
                }
            )
        if name == "github_list_issue_comments":
            return _text([{"id": 11, "user": "alice", "body": "please fix"}])
        if name == "github_get_repo":
            return _text(
                {
                    "full_name": f"{owner}/{repo}",
                    "default_branch": "master",
                    "private": False,
                }
            )
        if name == "github_list_commits":
            return _text(
                [
                    {"sha": "abc1234", "message": "init"},
                    {"sha": "def5678", "message": "fix"},
                ]
            )
        if name == "github_get_commit":
            return _text({"sha": args.get("sha"), "message": "fix", "files": ["calc.py"]})
        if name == "github_list_branches":
            return _text([{"name": "master"}, {"name": "fix/typeerror"}])
        if name == "github_list_pull_requests":
            return _text([{"number": 9, "title": "Fix TypeError", "draft": True, "state": "open"}])
        if name == "github_get_pull_request":
            return _text(
                {
                    "number": args.get("number"),
                    "title": "Fix TypeError",
                    "draft": True,
                    "head": "fix/typeerror",
                    "base": "master",
                }
            )
        if name == "github_list_workflow_runs":
            return _text(
                [
                    {
                        "id": 1001,
                        "name": "CI",
                        "status": "completed",
                        "conclusion": "success",
                    }
                ]
            )
        if name == "github_create_draft_pr":
            pr = {
                "number": 100 + len(self._draft_prs),
                "title": args.get("title"),
                "head": args.get("head"),
                "base": args.get("base") or "master",
                "body": args.get("body") or "",
                "draft": True,
                "html_url": f"https://github.com/{owner}/{repo}/pull/{100 + len(self._draft_prs)}",
            }
            self._draft_prs.append(pr)
            return _text(pr)
        if name in GITHUB_MCP_DENIED_TOOLS or name.startswith("github_merge"):
            return {"content": [{"type": "text", "text": "forbidden"}], "isError": True}
        return {"error": f"unknown tool {name}"}
