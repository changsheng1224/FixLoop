"""可执行 Skill 的纯函数 Runner（可注入 Tool）。"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Callable

ToolFn = Callable[[dict[str, Any]], str]

_ISSUE_URL_RE = re.compile(
    r"github\.com/(?P<owner>[\w.-]+)/(?P<repo>[\w.-]+)/issues/(?P<number>\d+)",
    re.I,
)
_ISSUE_HASH_RE = re.compile(r"(?:issue|PR)\s*#(?P<number>\d+)", re.I)
_OWNER_REPO_RE = re.compile(r"\b(?P<owner>[\w.-]+)/(?P<repo>[\w.-]+)\b")
_DIFF_FILE_RE = re.compile(r"(?m)^(?:---|\+\+\+|diff --git(?: a/| b/)?)([^\s]+)")


def _parse_jsonish(text: str) -> Any:
    text = (text or "").strip()
    if not text:
        return None
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return {"raw": text}


def parse_github_ref(text: str) -> dict[str, Any]:
    """从文本抽取 owner/repo/number。"""
    out: dict[str, Any] = {}
    m = _ISSUE_URL_RE.search(text or "")
    if m:
        out["owner"] = m.group("owner")
        out["repo"] = m.group("repo")
        out["number"] = int(m.group("number"))
        out["url"] = m.group(0)
        return out
    m2 = _ISSUE_HASH_RE.search(text or "")
    if m2:
        out["number"] = int(m2.group("number"))
    m3 = _OWNER_REPO_RE.search(text or "")
    if m3 and "github.com" not in (text or "").lower()[: m3.start() + 20]:
        # weak owner/repo only if no better signal
        out.setdefault("owner", m3.group("owner"))
        out.setdefault("repo", m3.group("repo"))
    return out


def run_github_issue_ingestion(
    args: dict[str, Any] | None = None,
    *,
    github_get_issue: ToolFn | None = None,
) -> dict[str, Any]:
    """Issue → IssueSpec。无远程 Tool 时仍返回结构化 stub（可测）。"""
    args = dict(args or {})
    text = str(args.get("text") or args.get("issue_text") or "")
    ref = parse_github_ref(text)
    owner = args.get("owner") or ref.get("owner") or "unknown"
    repo = args.get("repo") or ref.get("repo") or "unknown"
    number = args.get("number") if args.get("number") is not None else ref.get("number")
    issue_spec: dict[str, Any] = {
        "owner": owner,
        "repo": repo,
        "number": number,
        "title": "",
        "body": "",
        "state": "unknown",
        "html_url": ref.get("url")
        or (f"https://github.com/{owner}/{repo}/issues/{number}" if number else ""),
        "labels": [],
        "source": "stub",
    }
    if github_get_issue is not None and number is not None:
        raw = github_get_issue({"owner": owner, "repo": repo, "number": int(number)})
        parsed = _parse_jsonish(raw)
        if isinstance(parsed, dict):
            # MCP mock wraps as {"type":"text","text": "..."} sometimes
            if "text" in parsed and isinstance(parsed["text"], str):
                inner = _parse_jsonish(parsed["text"])
                if isinstance(inner, dict):
                    parsed = inner
            for key in ("title", "body", "state", "html_url", "labels", "number"):
                if key in parsed:
                    issue_spec[key] = parsed[key]
            issue_spec["source"] = "github_get_issue"
    elif text and not issue_spec["title"]:
        # 启发式：首行作 title
        first = text.strip().splitlines()[0][:200] if text.strip() else ""
        issue_spec["title"] = first
        issue_spec["body"] = text[:4000]
        issue_spec["source"] = "text_heuristic"
    evidence = ["issue_spec.title", "issue_spec.number"]
    return {
        "skill": "github_issue_ingestion",
        "issue_spec": issue_spec,
        "completion_evidence": evidence,
        "ok": bool(issue_spec.get("title") or issue_spec.get("number")),
    }


def run_stacktrace_localization(args: dict[str, Any] | None = None) -> dict[str, Any]:
    """错误栈 → localization（复用 stack_parse）。"""
    from src.tools.stack_parser import stack_parse

    args = dict(args or {})
    tb = str(args.get("traceback") or args.get("text") or "")
    raw = stack_parse(None, {"traceback": tb})
    parsed = _parse_jsonish(raw)
    localization: dict[str, Any]
    if isinstance(parsed, dict) and "exception_type" in parsed:
        localization = parsed
    else:
        localization = {
            "exception_type": "",
            "exception_message": "",
            "frames": [],
            "raw": raw,
        }
    frames = localization.get("frames") or []
    top_file = ""
    if frames and isinstance(frames[0], dict):
        top_file = str(frames[0].get("file") or frames[0].get("filename") or "")
    localization["top_file"] = top_file
    return {
        "skill": "stacktrace_localization",
        "localization": localization,
        "completion_evidence": ["localization.exception_type", "localization.frames"],
        "ok": bool(localization.get("exception_type") or frames),
    }


def _files_from_diff(diff: str) -> list[str]:
    files: list[str] = []
    for m in _DIFF_FILE_RE.finditer(diff or ""):
        path = m.group(1).lstrip("ab/")
        if path in ("/dev/null", "dev/null"):
            continue
        if path not in files:
            files.append(path)
    return files


def run_regression_test_selection(
    args: dict[str, Any] | None = None,
    *,
    find_test: ToolFn | None = None,
    workspace_root: str | Path | None = None,
) -> dict[str, Any]:
    """Diff / 变更文件 → 测试选择与 verify_scope。"""
    args = dict(args or {})
    diff = str(args.get("diff") or args.get("text") or "")
    files = list(args.get("changed_files") or []) or _files_from_diff(diff)
    function_name = str(args.get("function_name") or "")
    test_files: list[str] = []
    rationale: list[str] = []

    if find_test is not None and function_name and files:
        raw = find_test({"function_name": function_name, "file_path": files[0]})
        parsed = _parse_jsonish(raw)
        if isinstance(parsed, list):
            for item in parsed:
                if isinstance(item, dict) and item.get("test_file"):
                    test_files.append(str(item["test_file"]))
        rationale.append("find_test_tool")
    else:
        # 启发式：test_<module>.py
        for path in files:
            stem = Path(path).stem
            if stem.startswith("test_"):
                test_files.append(path)
            else:
                guess = f"tests/test_{stem}.py"
                if guess not in test_files:
                    test_files.append(guess)
        rationale.append("filename_heuristic")

    if workspace_root:
        root = Path(workspace_root)
        existing = [t for t in test_files if (root / t).is_file()]
        if existing:
            test_files = existing
            rationale.append("workspace_filter")

    verify_scope = "selected_tests" if test_files else "full_suite"
    if len(files) >= 3:
        verify_scope = "widened"
        rationale.append("many_files_widen")

    selection = {
        "changed_files": files,
        "test_files": test_files,
        "verify_scope": verify_scope,
        "rationale": ";".join(rationale),
    }
    return {
        "skill": "regression_test_selection",
        "selection": selection,
        "completion_evidence": ["selection.test_files", "selection.verify_scope"],
        "ok": True,
    }


def run_repo_code_search(
    args: dict[str, Any] | None = None,
    *,
    grep: ToolFn | None = None,
) -> dict[str, Any]:
    """符号 / 报错串搜索。"""
    args = dict(args or {})
    text = str(args.get("text") or "")
    query = str(args.get("query") or "").strip()
    if not query:
        m = re.search(
            r"(?i)(?:grep\s+for|find\s+definition\s+of|search\s+(?:code|symbol|repo)\s+for)\s+['\"]?([^\n'\"]+)",
            text,
        )
        query = (m.group(1).strip() if m else text.strip().splitlines()[0][:120]) if text.strip() else ""
    hits: list[dict[str, Any]] = []
    if grep is not None and query:
        raw = grep({"pattern": query})
        parsed = _parse_jsonish(raw)
        if isinstance(parsed, list):
            hits = [x for x in parsed if isinstance(x, dict)]
        elif isinstance(parsed, dict):
            hits = [parsed]
        else:
            hits = [{"raw": raw}]
    elif query:
        hits = [{"note": "heuristic_only", "query": query}]
    return {
        "skill": "repo_code_search",
        "search": {"query": query, "hits": hits},
        "completion_evidence": ["search.query", "search.hits"],
        "ok": bool(query),
    }


def run_baseline_verify(args: dict[str, Any] | None = None) -> dict[str, Any]:
    """修前基线验证计划（不强制真跑）。"""
    args = dict(args or {})
    cmd = str(args.get("test_command") or args.get("command") or "pytest -q")
    text = str(args.get("text") or "")
    expect_fail = True
    if re.search(r"(?i)\bexpect\s+pass\b|\balready\s+green\b", text):
        expect_fail = False
    return {
        "skill": "baseline_verify",
        "baseline": {
            "command": cmd,
            "expect_fail": expect_fail,
            "status": "planned",
            "phase": "pre_fix",
        },
        "completion_evidence": ["baseline.command", "baseline.status"],
        "ok": True,
    }


def run_patch_apply_check(args: dict[str, Any] | None = None) -> dict[str, Any]:
    """应用 patch 的检查计划 / 文件列表。"""
    args = dict(args or {})
    diff = str(args.get("diff") or args.get("text") or "")
    files = _files_from_diff(diff)
    dry = bool(re.search(r"(?i)dry[- ]?run", diff or str(args.get("text") or "")))
    status = "dry_run" if dry else ("ready" if files or diff.strip() else "missing_diff")
    checks = ["syntax_smoke"]
    if dry:
        checks.insert(0, "dry_run_apply")
    return {
        "skill": "patch_apply_check",
        "apply": {
            "files": files,
            "status": status,
            "checks": checks,
            "draft": False,
        },
        "completion_evidence": ["apply.status", "apply.files"],
        "ok": status != "missing_diff",
    }


def run_draft_pr_prepare(args: dict[str, Any] | None = None) -> dict[str, Any]:
    """Draft PR 元数据准备（draft 强制 true）。"""
    args = dict(args or {})
    text = str(args.get("text") or "")
    title = str(args.get("title") or "").strip()
    if not title:
        m = re.search(r"(?i)title\s*[:=]\s*(.+)", text)
        title = m.group(1).strip()[:120] if m else "fix: automated repair"
    body = str(args.get("body") or text[:2000] or "Automated FixLoop repair.")
    base = str(args.get("base") or "master")
    head = str(args.get("head") or "fixloop/repair")
    return {
        "skill": "draft_pr_prepare",
        "draft_pr": {
            "title": title,
            "body": body,
            "base": base,
            "head": head,
            "draft": True,
        },
        "completion_evidence": ["draft_pr.title", "draft_pr.draft"],
        "ok": True,
    }


RUNNERS: dict[str, Callable[..., dict[str, Any]]] = {
    "github_issue_ingestion": run_github_issue_ingestion,
    "stacktrace_localization": run_stacktrace_localization,
    "regression_test_selection": run_regression_test_selection,
    "repo_code_search": run_repo_code_search,
    "baseline_verify": run_baseline_verify,
    "patch_apply_check": run_patch_apply_check,
    "draft_pr_prepare": run_draft_pr_prepare,
}


def run_executable_skill(name: str, args: dict[str, Any] | None = None, **kwargs: Any) -> dict[str, Any]:
    fn = RUNNERS.get(name)
    if fn is None:
        raise KeyError(f"no runner for skill: {name}")
    return fn(args, **kwargs)
