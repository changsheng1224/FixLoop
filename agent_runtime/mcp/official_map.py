"""官方 GitHub MCP 工具名 / 参数 ↔ FixLoop ``github_*`` 本地契约。"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

# FixLoop 本地 schema（与 Mock 对齐，面向 Agent；不暴露官方 method 细节）
_OWNER_REPO = {
    "owner": {"type": "string"},
    "repo": {"type": "string"},
}


@dataclass(frozen=True)
class OfficialToolMap:
    """一条本地工具到官方工具的映射。"""

    local_name: str
    remote_name: str
    description: str
    properties: dict[str, Any]
    required: list[str]
    adapt_args: Callable[[dict[str, Any]], dict[str, Any]]


def _passthrough(args: dict[str, Any]) -> dict[str, Any]:
    return dict(args)


def _adapt_get_issue(args: dict[str, Any]) -> dict[str, Any]:
    out = {
        "owner": args["owner"],
        "repo": args["repo"],
        "issue_number": int(args["number"]),
        "method": "get",
    }
    return out


def _adapt_list_issue_comments(args: dict[str, Any]) -> dict[str, Any]:
    return {
        "owner": args["owner"],
        "repo": args["repo"],
        "issue_number": int(args["number"]),
        "method": "get_comments",
    }


def _adapt_get_repo(args: dict[str, Any]) -> dict[str, Any]:
    # 官方无独立 get_repo；用根目录 listing 作为只读仓库探测
    return {"owner": args["owner"], "repo": args["repo"], "path": ""}


def _adapt_list_commits(args: dict[str, Any]) -> dict[str, Any]:
    out = {"owner": args["owner"], "repo": args["repo"]}
    if args.get("sha"):
        out["sha"] = args["sha"]
    limit = args.get("limit")
    if limit is not None:
        out["perPage"] = int(limit)
    return out


def _adapt_list_branches(args: dict[str, Any]) -> dict[str, Any]:
    out = {"owner": args["owner"], "repo": args["repo"]}
    if args.get("limit") is not None:
        out["perPage"] = int(args["limit"])
    return out


def _adapt_list_issues(args: dict[str, Any]) -> dict[str, Any]:
    out = {"owner": args["owner"], "repo": args["repo"]}
    if args.get("state"):
        out["state"] = args["state"]
    if args.get("limit") is not None:
        out["perPage"] = int(args["limit"])
    return out


def _adapt_list_prs(args: dict[str, Any]) -> dict[str, Any]:
    out = {"owner": args["owner"], "repo": args["repo"]}
    if args.get("state"):
        out["state"] = args["state"]
    return out


def _adapt_get_pr(args: dict[str, Any]) -> dict[str, Any]:
    return {
        "owner": args["owner"],
        "repo": args["repo"],
        "pullNumber": int(args["number"]),
        "method": "get",
    }


def _adapt_list_workflow_runs(args: dict[str, Any]) -> dict[str, Any]:
    out = {
        "owner": args["owner"],
        "repo": args["repo"],
        "method": "list_workflow_runs",
    }
    if args.get("limit") is not None:
        out["per_page"] = int(args["limit"])
    return out


def _adapt_create_draft_pr(args: dict[str, Any]) -> dict[str, Any]:
    out = {
        "owner": args["owner"],
        "repo": args["repo"],
        "title": args["title"],
        "head": args["head"],
        "base": args.get("base") or "master",
        "draft": True,  # 强制 draft，唯一写路径
    }
    if args.get("body"):
        out["body"] = args["body"]
    return out


OFFICIAL_TOOL_MAPS: tuple[OfficialToolMap, ...] = (
    OfficialToolMap(
        local_name="github_list_issues",
        remote_name="list_issues",
        description="List issues in a repository (GitHub MCP)",
        properties={
            **_OWNER_REPO,
            "state": {"type": "string", "default": "open"},
            "limit": {"type": "integer", "default": 20},
        },
        required=["owner", "repo"],
        adapt_args=_adapt_list_issues,
    ),
    OfficialToolMap(
        local_name="github_get_issue",
        remote_name="issue_read",
        description="Get a single issue (GitHub MCP issue_read/get)",
        properties={**_OWNER_REPO, "number": {"type": "integer"}},
        required=["owner", "repo", "number"],
        adapt_args=_adapt_get_issue,
    ),
    OfficialToolMap(
        local_name="github_list_issue_comments",
        remote_name="issue_read",
        description="List comments on an issue (GitHub MCP issue_read/get_comments)",
        properties={**_OWNER_REPO, "number": {"type": "integer"}},
        required=["owner", "repo", "number"],
        adapt_args=_adapt_list_issue_comments,
    ),
    OfficialToolMap(
        local_name="github_get_repo",
        remote_name="get_file_contents",
        description="Probe repository via root listing (GitHub MCP get_file_contents)",
        properties=dict(_OWNER_REPO),
        required=["owner", "repo"],
        adapt_args=_adapt_get_repo,
    ),
    OfficialToolMap(
        local_name="github_list_commits",
        remote_name="list_commits",
        description="List commits (GitHub MCP)",
        properties={
            **_OWNER_REPO,
            "sha": {"type": "string", "default": ""},
            "limit": {"type": "integer", "default": 10},
        },
        required=["owner", "repo"],
        adapt_args=_adapt_list_commits,
    ),
    OfficialToolMap(
        local_name="github_get_commit",
        remote_name="get_commit",
        description="Get a commit by sha (GitHub MCP)",
        properties={**_OWNER_REPO, "sha": {"type": "string"}},
        required=["owner", "repo", "sha"],
        adapt_args=_passthrough,
    ),
    OfficialToolMap(
        local_name="github_list_branches",
        remote_name="list_branches",
        description="List branches (GitHub MCP)",
        properties={**_OWNER_REPO, "limit": {"type": "integer", "default": 20}},
        required=["owner", "repo"],
        adapt_args=_adapt_list_branches,
    ),
    OfficialToolMap(
        local_name="github_list_pull_requests",
        remote_name="list_pull_requests",
        description="List pull requests (GitHub MCP)",
        properties={**_OWNER_REPO, "state": {"type": "string", "default": "open"}},
        required=["owner", "repo"],
        adapt_args=_adapt_list_prs,
    ),
    OfficialToolMap(
        local_name="github_get_pull_request",
        remote_name="pull_request_read",
        description="Get a pull request (GitHub MCP pull_request_read/get)",
        properties={**_OWNER_REPO, "number": {"type": "integer"}},
        required=["owner", "repo", "number"],
        adapt_args=_adapt_get_pr,
    ),
    OfficialToolMap(
        local_name="github_list_workflow_runs",
        remote_name="actions_list",
        description="List workflow runs (GitHub MCP actions_list)",
        properties={**_OWNER_REPO, "limit": {"type": "integer", "default": 10}},
        required=["owner", "repo"],
        adapt_args=_adapt_list_workflow_runs,
    ),
    OfficialToolMap(
        local_name="github_create_draft_pr",
        remote_name="create_pull_request",
        description="Create a draft pull request (GitHub MCP; draft forced)",
        properties={
            **_OWNER_REPO,
            "title": {"type": "string"},
            "head": {"type": "string"},
            "base": {"type": "string", "default": "master"},
            "body": {"type": "string", "default": ""},
        },
        required=["owner", "repo", "title", "head"],
        adapt_args=_adapt_create_draft_pr,
    ),
)

OFFICIAL_MAP_BY_LOCAL: dict[str, OfficialToolMap] = {
    m.local_name: m for m in OFFICIAL_TOOL_MAPS
}

# 官方侧危险工具（即使 list 出来也不经本地名暴露）
OFFICIAL_DENIED_REMOTE_TOOLS: frozenset[str] = frozenset(
    {
        "merge_pull_request",
        "delete_file",
        "push_files",
        "fork_repository",
        "create_repository",
        "delete_repository",
        "update_pull_request_branch",
        "request_copilot_review",
        "create_or_update_file",
        "actions_run_trigger",
    }
)


def adapt_local_call(
    local_name: str, arguments: dict[str, Any] | None
) -> tuple[str, dict[str, Any]]:
    """本地工具名+参数 → (官方工具名, 官方参数)。"""
    mapping = OFFICIAL_MAP_BY_LOCAL.get(local_name)
    if mapping is None:
        raise KeyError(local_name)
    return mapping.remote_name, mapping.adapt_args(dict(arguments or {}))
