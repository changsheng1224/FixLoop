"""定位/检索质量：栈接地、路径归一、嫌疑排序与噪声抑制。

能力向（非单例）：
- 从 issue traceback 确定性抽出嫌疑帧
- 只保留仓库内真实存在的实现文件（测试帧降权）
- LLM suspects 与栈 suspects 合并后按证据排序截断
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import TYPE_CHECKING

from src.state import SuspectLocation

if TYPE_CHECKING:
    from src.state import RepairPlan

__all__ = [
    "refine_suspects",
    "suspects_from_issue",
    "normalize_repo_path",
    "score_suspect",
    "retrieve_keywords",
    "has_grounded_impl_suspect",
    "ensure_grounded_suspects",
]


_TEST_PATH_HINTS = (
    "/tests/",
    "\\tests\\",
    "/test/",
    "\\test\\",
    "/testing/",
    "test_",
)
_SKIP_PATH_PARTS = (
    "site-packages",
    "dist-packages",
    ".venv",
    "/venv/",
    "\\venv\\",
    "lib/python",
    "Lib\\",
)


def normalize_repo_path(raw: str, repo_root: str | Path) -> str | None:
    """把绝对/杂乱路径收敛为仓库相对路径；不存在则 None。"""
    if not raw:
        return None
    text = raw.strip().replace("\\", "/").strip("\"'`")
    if not text.endswith(".py") and ".py:" in text:
        text = text.split(".py:", 1)[0] + ".py"
    lower = text.lower()
    if any(p.lower().replace("\\", "/") in lower for p in _SKIP_PATH_PARTS):
        return None

    root = Path(repo_root)
    cand = Path(text)
    if cand.is_absolute():
        try:
            rel = cand.resolve().relative_to(root.resolve())
            text = str(rel).replace("\\", "/")
        except (ValueError, OSError):
            # 尝试用文件名在仓内搜（仅当 basename 唯一性不要求：取相对后缀匹配）
            name = cand.name
            if not name.endswith(".py"):
                return None
            # 用路径尾部若干段匹配
            parts = [p for p in cand.parts if p not in ("/",)]
            for n in range(min(4, len(parts)), 0, -1):
                tail = "/".join(parts[-n:]).replace("\\", "/")
                hit = root / tail
                if hit.is_file():
                    return tail
            return None

    text = text.lstrip("./")
    if (root / text).is_file():
        return text
    # 尾段匹配
    parts = text.split("/")
    for n in range(min(4, len(parts)), 0, -1):
        tail = "/".join(parts[-n:])
        if (root / tail).is_file():
            return tail
    return None


def _is_test_path(path: str) -> bool:
    p = path.replace("\\", "/")
    base = Path(p).name
    if base.startswith("test_") or base.endswith("_test.py"):
        return True
    lowered = f"/{p.lower()}/"
    return any(h.replace("\\", "/") in lowered for h in ("/tests/", "/test/", "/testing/"))


def suspects_from_issue(issue: str, repo_root: str | Path) -> list[SuspectLocation]:
    """确定性：traceback / issue 路径 → SuspectLocation（优先非测试应用帧）。"""
    from src.tools.stack_parser import stack_parse

    frames: list[dict] = []
    try:
        raw = stack_parse(None, {"traceback": issue or ""})
        data = json.loads(raw) if raw and not str(raw).startswith("Error") else {}
        frames = list(data.get("frames") or [])
        if data.get("exception_type") == "SyntaxError" and data.get("syntax_file"):
            frames.append(
                {
                    "file": data["syntax_file"],
                    "line": int(data.get("syntax_line") or 1),
                    "function": "<syntax>",
                }
            )
    except Exception:
        frames = []

    from src.repair.issue_paths import extract_paths_from_issue

    path_hints = extract_paths_from_issue(issue or "")

    out: list[SuspectLocation] = []
    seen: set[tuple[str, int]] = set()

    # 自栈底向上（更靠近抛出点）
    for fr in reversed(frames):
        rel = normalize_repo_path(str(fr.get("file") or ""), repo_root)
        if not rel:
            continue
        line = int(fr.get("line") or 1)
        key = (rel, line)
        if key in seen:
            continue
        seen.add(key)
        out.append(
            SuspectLocation(
                file_path=rel,
                start_line=line,
                end_line=line,
                function_name=(fr.get("function") or None),
                reason="堆栈指向",
                confidence=0.92 if not _is_test_path(rel) else 0.55,
            )
        )

    for path in path_hints:
        rel = normalize_repo_path(path, repo_root)
        if not rel:
            continue
        key = (rel, 1)
        # 已有同文件更精确行号则跳过粗路径
        if any(s.file_path == rel for s in out):
            continue
        if key in seen:
            continue
        seen.add(key)
        out.append(
            SuspectLocation(
                file_path=rel,
                start_line=1,
                end_line=1,
                reason="issue 路径",
                confidence=0.75 if not _is_test_path(rel) else 0.45,
            )
        )
    return out


def score_suspect(
    suspect: SuspectLocation,
    *,
    repo_root: str | Path,
    issue_paths: set[str],
    stack_files: set[str],
    plan_files: set[str],
) -> float:
    path = (suspect.file_path or "").replace("\\", "/")
    score = float(suspect.confidence or 0.0)
    if not path:
        return -1.0
    if not (Path(repo_root) / path).is_file():
        return -1.0
    if path in stack_files:
        score += 0.35
    if path in issue_paths:
        score += 0.2
    if path in plan_files:
        score += 0.15
    if _is_test_path(path):
        score -= 0.35
    if suspect.function_name:
        score += 0.05
    if suspect.reason and "堆栈" in suspect.reason:
        score += 0.1
    if suspect.reason in ("测试导入", "语义扩展", "issue 符号", "grep命中"):
        score += 0.12
    if suspect.reason in ("F2P覆盖", "test_patch覆盖", "localize_confirmed"):
        score += 0.2
    if suspect.reason == "调用方扩展":
        score += 0.05
    return score


def refine_suspects(
    suspects: list[SuspectLocation] | None,
    issue: str,
    repo_root: str | Path,
    plan: "RepairPlan | None" = None,
    *,
    max_keep: int = 8,
    related_tests: list[str] | None = None,
    fail_nodeids: list[str] | None = None,
    enable_semantic_expand: bool = True,
) -> list[SuspectLocation]:
    """合并栈接地 + LLM/降级嫌疑 + 语义多跳扩展，排序截断。"""
    grounded = suspects_from_issue(issue or "", repo_root)
    merged: list[SuspectLocation] = []
    seen_files_lines: set[tuple[str, int, str]] = set()

    def _add(s: SuspectLocation) -> None:
        rel = normalize_repo_path(s.file_path or "", repo_root)
        if not rel:
            return
        start = int(s.start_line or 1)
        func = s.function_name or ""
        key = (rel, start, func)
        if key in seen_files_lines:
            return
        soft = (rel, 0, func)
        if func and any(k[0] == rel and k[2] == func for k in seen_files_lines):
            return
        seen_files_lines.add(key)
        if soft[2]:
            seen_files_lines.add(soft)
        merged.append(
            SuspectLocation(
                file_path=rel,
                start_line=start,
                end_line=max(start, int(s.end_line or start)),
                function_name=s.function_name,
                class_name=s.class_name,
                reason=s.reason or "",
                confidence=float(s.confidence or 0.0),
            )
        )

    for s in grounded:
        _add(s)
    for s in suspects or []:
        _add(s)

    if enable_semantic_expand:
        try:
            from src.repair.localize_expand import expand_suspects_semantic

            extra = expand_suspects_semantic(
                merged,
                repo_root=repo_root,
                issue=issue or "",
                related_tests=related_tests,
                fail_nodeids=fail_nodeids,
                max_new=6,
            )
            for s in extra:
                _add(s)
        except Exception:
            pass
        try:
            from src.repair.symbol_index import boost_suspects_from_index

            for s in boost_suspects_from_index(
                repo_root=repo_root,
                issue=issue or "",
                related_tests=related_tests,
                fail_nodeids=fail_nodeids,
                max_new=6,
            ):
                _add(s)
        except Exception:
            pass

    from src.repair.issue_paths import extract_paths_from_issue

    issue_paths = {
        p
        for p in (
            normalize_repo_path(x, repo_root) for x in extract_paths_from_issue(issue or "")
        )
        if p
    }
    stack_files = {s.file_path for s in grounded}
    plan_files = {str(f).replace("\\", "/") for f in (plan.suspect_files if plan else [])}

    ranked = sorted(
        merged,
        key=lambda s: score_suspect(
            s,
            repo_root=repo_root,
            issue_paths=issue_paths,
            stack_files=stack_files,
            plan_files=plan_files,
        ),
        reverse=True,
    )
    kept = [
        s
        for s in ranked
        if score_suspect(
            s,
            repo_root=repo_root,
            issue_paths=issue_paths,
            stack_files=stack_files,
            plan_files=plan_files,
        )
        >= 0
    ][:max_keep]

    if not kept:
        for s in grounded + list(suspects or []):
            rel = normalize_repo_path(getattr(s, "file_path", "") or "", repo_root)
            if rel:
                kept.append(
                    SuspectLocation(
                        file_path=rel,
                        start_line=int(getattr(s, "start_line", 1) or 1),
                        end_line=int(getattr(s, "end_line", 1) or 1),
                        function_name=getattr(s, "function_name", None),
                        reason=getattr(s, "reason", "") or "fallback",
                        confidence=0.4,
                    )
                )
                if len(kept) >= max_keep:
                    break
    return kept


_IDENT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{2,}$")
_STOP_KEYWORDS = frozenset(
    {
        "the",
        "and",
        "for",
        "from",
        "this",
        "that",
        "with",
        "true",
        "false",
        "none",
        "self",
        "test",
        "tests",
        "error",
        "exception",
        "traceback",
        "file",
        "line",
        "return",
        "import",
        "class",
        "def",
        "args",
        "kwargs",
    }
)


def retrieve_keywords(
    suspects: list[SuspectLocation] | None,
    issue: str = "",
    *,
    max_keywords: int = 8,
) -> list[str]:
    """规则检索关键词：嫌疑函数/类 + 栈函数名；抑制 issue 里乱引号噪声。"""
    kws: list[str] = []
    seen: set[str] = set()

    def add(name: str) -> None:
        n = (name or "").strip()
        if not n or n.lower() in _STOP_KEYWORDS:
            return
        if not _IDENT_RE.match(n):
            return
        if n in seen:
            return
        seen.add(n)
        kws.append(n)

    for s in suspects or []:
        add(getattr(s, "function_name", "") or "")
        add(getattr(s, "class_name", "") or "")

    try:
        from src.tools.stack_parser import stack_parse

        raw = stack_parse(None, {"traceback": issue or ""})
        data = json.loads(raw) if raw and not str(raw).startswith("Error") else {}
        for fr in data.get("frames") or []:
            add(str(fr.get("function") or ""))
    except Exception:
        pass

    for m in re.finditer(r"\bdef\s+([A-Za-z_]\w*)", issue or ""):
        add(m.group(1))

    return kws[:max_keywords]


def has_grounded_impl_suspect(
    suspects: list[SuspectLocation] | None,
    repo_root: str | Path,
) -> bool:
    from src.repair.symbol_index import has_grounded_impl_suspect as _has

    return _has(suspects, repo_root)


def ensure_grounded_suspects(
    suspects: list[SuspectLocation] | None,
    *,
    repo_root: str | Path,
    issue: str = "",
    plan: "RepairPlan | None" = None,
    related_tests: list[str] | None = None,
    fail_nodeids: list[str] | None = None,
    max_keep: int = 8,
) -> tuple[list[SuspectLocation], bool]:
    """若尚未接地，用符号索引再抬一轮；返回 (suspects, boosted)。"""
    current = list(suspects or [])
    if has_grounded_impl_suspect(current, repo_root):
        return current, False
    from src.repair.symbol_index import boost_suspects_from_index

    boosted = boost_suspects_from_index(
        repo_root=repo_root,
        issue=issue,
        related_tests=related_tests,
        fail_nodeids=fail_nodeids,
        max_new=8,
    )
    if not boosted:
        return current, False
    merged = refine_suspects(
        current + boosted,
        issue,
        repo_root,
        plan=plan,
        max_keep=max_keep,
        related_tests=related_tests,
        fail_nodeids=fail_nodeids,
        enable_semantic_expand=False,  # 避免递归再扫
    )
    return merged, True
