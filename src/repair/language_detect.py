"""RepairPlan.language 规则检测（shebang / 扩展名 / 关键字）。"""

from __future__ import annotations

import re
from pathlib import Path

DEFAULT_LANGUAGE = "python"

EXTENSION_LANGUAGE: dict[str, str] = {
    ".py": "python",
    ".pyw": "python",
    ".js": "javascript",
    ".mjs": "javascript",
    ".cjs": "javascript",
    ".ts": "typescript",
    ".tsx": "typescript",
    ".java": "java",
    ".go": "go",
    ".rs": "rust",
    ".rb": "ruby",
    ".php": "php",
    ".cs": "csharp",
    ".kt": "kotlin",
    ".swift": "swift",
}

KEYWORD_LANGUAGES: list[tuple[str, str]] = [
    (r"\bpytest\b", "python"),
    (r"Traceback \(most recent call last\)", "python"),
    (r"ModuleNotFoundError|ImportError|TypeError", "python"),
    (r"java\.lang\.", "java"),
    (r"NullPointerException", "java"),
    (r"\bpanic:", "go"),
    (r"npm ERR!", "javascript"),
    (r"node:internal", "javascript"),
]

SHEBANG_INTERPRETERS: list[tuple[str, str]] = [
    (r"python", "python"),
    (r"node", "javascript"),
    (r"ruby", "ruby"),
    (r"php", "php"),
]

EXPLICIT_LANG_RE = re.compile(
    r"\[lang:(?P<lang>[\w+#]+)\]|language:\s*(?P<lang2>[\w+#]+)",
    re.IGNORECASE,
)

_PATH_IN_ISSUE_RE = re.compile(
    r'File\s+"([^"]+)"|at\s+(\S+\.\w+)',
)


def _normalize_language(raw: str) -> str:
    lang = raw.lower().strip()
    if lang in ("cs", "c#"):
        return "csharp"
    return lang


def _language_from_extension(path: str) -> str | None:
    ext = Path(path.replace("\\", "/")).suffix.lower()
    return EXTENSION_LANGUAGE.get(ext)


def _shebang_language(line: str) -> str | None:
    if not line.strip().startswith("#!"):
        return None
    for pattern, lang in SHEBANG_INTERPRETERS:
        if re.search(pattern, line, re.IGNORECASE):
            return lang
    return None


def _read_shebang_from_repo(repo_root: Path, suspect_files: list[str]) -> str | None:
    for rel in suspect_files:
        candidates = [repo_root / rel, repo_root / Path(rel).name]
        for path in candidates:
            if not path.is_file():
                continue
            try:
                first = path.read_text(encoding="utf-8", errors="ignore").splitlines()[0]
            except OSError:
                continue
            if first.startswith("#!"):
                return first
    return None


def detect_repair_language(
    issue: str,
    *,
    suspect_files: list[str] | None = None,
    repo_root: Path | str | None = None,
) -> tuple[str, str]:
    """从 issue / 嫌疑文件推断语言，返回 ``(language, source)``。"""
    scores: dict[str, int] = {}
    source_best: dict[str, tuple[int, str]] = {}

    def add(lang: str, weight: int, source: str) -> None:
        normalized = _normalize_language(lang)
        if not normalized:
            return
        scores[normalized] = scores.get(normalized, 0) + weight
        prev = source_best.get(normalized)
        if prev is None or weight >= prev[0]:
            source_best[normalized] = (weight, source)

    explicit = EXPLICIT_LANG_RE.search(issue)
    if explicit:
        raw = explicit.group("lang") or explicit.group("lang2") or ""
        add(raw, 100, "explicit")

    paths: list[str] = list(suspect_files or [])
    for file_match, at_match in _PATH_IN_ISSUE_RE.findall(issue):
        path = (file_match or at_match).replace("\\", "/")
        if path and path not in paths:
            paths.append(path)

    for path in paths:
        lang = _language_from_extension(path)
        if lang:
            add(lang, 60, f"extension:{Path(path).suffix.lower()}")

    for line in issue.splitlines()[:5]:
        lang = _shebang_language(line)
        if lang:
            add(lang, 80, "shebang:issue")

    if repo_root:
        shebang = _read_shebang_from_repo(Path(repo_root), paths)
        if shebang:
            lang = _shebang_language(shebang)
            if lang:
                add(lang, 80, "shebang:file")

    for pattern, lang in KEYWORD_LANGUAGES:
        if re.search(pattern, issue, re.IGNORECASE):
            add(lang, 40, f"keyword:{lang}")

    if not scores:
        return DEFAULT_LANGUAGE, "default"

    best_lang = max(scores, key=lambda k: scores[k])
    return best_lang, source_best[best_lang][1]
