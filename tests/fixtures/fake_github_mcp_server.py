"""最小假 MCP server（stdio NDJSON），供 StdioTransport / 官方映射单测。"""

from __future__ import annotations

import json
import sys

TOOLS = [
    {
        "name": "list_issues",
        "description": "List issues",
        "inputSchema": {
            "type": "object",
            "properties": {
                "owner": {"type": "string"},
                "repo": {"type": "string"},
                "state": {"type": "string"},
                "perPage": {"type": "number"},
            },
            "required": ["owner", "repo"],
        },
    },
    {
        "name": "issue_read",
        "description": "Read issue",
        "inputSchema": {
            "type": "object",
            "properties": {
                "owner": {"type": "string"},
                "repo": {"type": "string"},
                "issue_number": {"type": "number"},
                "method": {"type": "string"},
            },
            "required": ["owner", "repo", "issue_number", "method"],
        },
    },
    {
        "name": "get_file_contents",
        "description": "Get file contents",
        "inputSchema": {
            "type": "object",
            "properties": {
                "owner": {"type": "string"},
                "repo": {"type": "string"},
                "path": {"type": "string"},
            },
            "required": ["owner", "repo"],
        },
    },
    {
        "name": "list_commits",
        "description": "List commits",
        "inputSchema": {
            "type": "object",
            "properties": {
                "owner": {"type": "string"},
                "repo": {"type": "string"},
                "sha": {"type": "string"},
                "perPage": {"type": "number"},
            },
            "required": ["owner", "repo"],
        },
    },
    {
        "name": "get_commit",
        "description": "Get commit",
        "inputSchema": {
            "type": "object",
            "properties": {
                "owner": {"type": "string"},
                "repo": {"type": "string"},
                "sha": {"type": "string"},
            },
            "required": ["owner", "repo", "sha"],
        },
    },
    {
        "name": "list_branches",
        "description": "List branches",
        "inputSchema": {
            "type": "object",
            "properties": {
                "owner": {"type": "string"},
                "repo": {"type": "string"},
                "perPage": {"type": "number"},
            },
            "required": ["owner", "repo"],
        },
    },
    {
        "name": "list_pull_requests",
        "description": "List PRs",
        "inputSchema": {
            "type": "object",
            "properties": {
                "owner": {"type": "string"},
                "repo": {"type": "string"},
                "state": {"type": "string"},
            },
            "required": ["owner", "repo"],
        },
    },
    {
        "name": "pull_request_read",
        "description": "Read PR",
        "inputSchema": {
            "type": "object",
            "properties": {
                "owner": {"type": "string"},
                "repo": {"type": "string"},
                "pullNumber": {"type": "number"},
                "method": {"type": "string"},
            },
            "required": ["owner", "repo", "pullNumber", "method"],
        },
    },
    {
        "name": "actions_list",
        "description": "List actions",
        "inputSchema": {
            "type": "object",
            "properties": {
                "owner": {"type": "string"},
                "repo": {"type": "string"},
                "method": {"type": "string"},
                "per_page": {"type": "number"},
            },
            "required": ["owner", "repo", "method"],
        },
    },
    {
        "name": "create_pull_request",
        "description": "Create PR",
        "inputSchema": {
            "type": "object",
            "properties": {
                "owner": {"type": "string"},
                "repo": {"type": "string"},
                "title": {"type": "string"},
                "head": {"type": "string"},
                "base": {"type": "string"},
                "body": {"type": "string"},
                "draft": {"type": "boolean"},
            },
            "required": ["owner", "repo", "title", "head", "base"],
        },
    },
    {
        "name": "merge_pull_request",
        "description": "Merge PR (should not be mapped)",
        "inputSchema": {
            "type": "object",
            "properties": {
                "owner": {"type": "string"},
                "repo": {"type": "string"},
                "pullNumber": {"type": "number"},
            },
            "required": ["owner", "repo", "pullNumber"],
        },
    },
]


def _ok(req_id, result):
    sys.stdout.write(json.dumps({"jsonrpc": "2.0", "id": req_id, "result": result}) + "\n")
    sys.stdout.flush()


def _err(req_id, message):
    sys.stdout.write(
        json.dumps(
            {
                "jsonrpc": "2.0",
                "id": req_id,
                "error": {"code": -32000, "message": message},
            }
        )
        + "\n"
    )
    sys.stdout.flush()


def _text(payload) -> dict:
    body = payload if isinstance(payload, str) else json.dumps(payload, ensure_ascii=False)
    return {"content": [{"type": "text", "text": body}], "isError": False}


def main() -> None:
    for raw in sys.stdin:
        line = raw.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(msg, dict):
            continue
        method = msg.get("method")
        req_id = msg.get("id")
        params = msg.get("params") or {}
        if method == "notifications/initialized" or req_id is None:
            continue
        if method == "initialize":
            _ok(
                req_id,
                {
                    "protocolVersion": params.get("protocolVersion") or "2024-11-05",
                    "capabilities": {"tools": {}},
                    "serverInfo": {"name": "fake-github-mcp", "version": "0.0.1"},
                },
            )
            continue
        if method == "tools/list":
            _ok(req_id, {"tools": TOOLS})
            continue
        if method == "tools/call":
            name = params.get("name")
            args = params.get("arguments") or {}
            if name == "list_issues":
                _ok(req_id, _text([{"number": 1, "title": "bug"}]))
            elif name == "issue_read":
                _ok(
                    req_id,
                    _text(
                        {
                            "method": args.get("method"),
                            "issue_number": args.get("issue_number"),
                            "title": "bug",
                        }
                    ),
                )
            elif name == "create_pull_request":
                _ok(
                    req_id,
                    _text(
                        {
                            "draft": args.get("draft"),
                            "title": args.get("title"),
                            "head": args.get("head"),
                            "base": args.get("base"),
                        }
                    ),
                )
            elif name == "get_file_contents":
                _ok(req_id, _text({"path": args.get("path", ""), "entries": ["README.md"]}))
            else:
                _ok(req_id, _text({"ok": True, "tool": name, "args": args}))
            continue
        _err(req_id, f"unknown method {method}")


if __name__ == "__main__":
    main()
