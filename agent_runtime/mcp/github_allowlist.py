"""GitHub MCP 工具允许/拒绝名单（本地名 = MCP tool name）。"""

from __future__ import annotations

# 只读开放集（第一版）
GITHUB_MCP_READ_TOOLS: frozenset[str] = frozenset(
    {
        "github_list_issues",
        "github_get_issue",
        "github_list_issue_comments",
        "github_get_repo",
        "github_list_commits",
        "github_get_commit",
        "github_list_branches",
        "github_list_pull_requests",
        "github_get_pull_request",
        "github_list_workflow_runs",
    }
)

# 唯一写操作（须人工确认）
GITHUB_MCP_WRITE_TOOLS: frozenset[str] = frozenset(
    {
        "github_create_draft_pr",
    }
)

# 显式禁止（即使远端 list 出来也不注册）
GITHUB_MCP_DENIED_TOOLS: frozenset[str] = frozenset(
    {
        "github_merge_pull_request",
        "github_delete_branch",
        "github_update_repo_secrets",
        "github_delete_repository",
        "github_update_repository",
        "github_add_collaborator",
        "merge_pull_request",
        "delete_branch",
        "update_repo_secrets",
    }
)

GITHUB_MCP_ALLOWED_TOOLS: frozenset[str] = GITHUB_MCP_READ_TOOLS | GITHUB_MCP_WRITE_TOOLS


def is_github_mcp_tool_allowed(name: str) -> bool:
    if name in GITHUB_MCP_DENIED_TOOLS:
        return False
    return name in GITHUB_MCP_ALLOWED_TOOLS
