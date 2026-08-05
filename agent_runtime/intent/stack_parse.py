"""Parse Python/Java-ish tracebacks from mixed user input (prose + code + stack).

When the user pastes a large source blob *and* a Traceback, prefer frames from the
traceback region so pasted ``*.py`` identifiers do not pollute ``suspect_files``.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

_TRACEBACK_START = re.compile(
    r"(?im)^(?P<head>\s*Traceback \(most recent call last\):)"
)
# Also accept leading File frames without the Traceback header (common paste).
_FILE_FRAME = re.compile(
    r'(?m)^\s*File\s+"(?P<file>[^"]+)",\s*line\s+(?P<line>\d+)(?:,\s+in\s+(?P<func>\S+))?'
)
_EXCEPTION_LINE = re.compile(
    r"(?m)^(?P<etype>"
    r"ModuleNotFoundError|ImportError|TypeError|AttributeError|NameError|"
    r"KeyError|ValueError|AssertionError|SyntaxError|RuntimeError|IndexError|"
    r"ZeroDivisionError|FileNotFoundError|OSError|Exception"
    r")\s*:\s*(?P<msg>.*)$"
)
_AT_FILE = re.compile(r"(?i)\bat\s+(?P<file>[\w./\\-]+\.py):(?P<line>\d+)\b")
_CANDIDATE_FILES = re.compile(
    r"(?i)Candidate source files:\s*(?P<body>.+)"
)
_CODE_FENCE = re.compile(r"```[\w+-]*\n.*?```", re.S)
# Bare .py tokens (noisy inside pasted source).
_BARE_PY = re.compile(r"(?i)\b([\w./\\-]+\.py)\b")

_ISSUE_TYPE_MAP = {
    "typeerror": "type_error",
    "importerror": "import_error",
    "modulenotfounderror": "import_error",
    "attributeerror": "attribute_error",
    "nameerror": "name_error",
    "keyerror": "key_error",
    "valueerror": "value_error",
    "assertionerror": "test_failure",
    "syntaxerror": "syntax_error",
    "runtimeerror": "runtime_error",
}


@dataclass
class StackFrame:
    file: str
    line: int
    func: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {"file": self.file, "line": self.line, "func": self.func}


@dataclass
class StackParseResult:
    has_traceback: bool = False
    stack_text: str = ""
    stack_start: int = -1
    stack_end: int = -1
    frames: list[StackFrame] = field(default_factory=list)
    exception_type: str = ""
    exception_msg: str = ""
    issue_type: str = ""
    suspect_files: list[str] = field(default_factory=list)

    @property
    def top_frame(self) -> StackFrame | None:
        return self.frames[-1] if self.frames else None

    def to_slots(self) -> dict[str, Any]:
        slots: dict[str, Any] = {}
        if self.suspect_files:
            slots["suspect_files"] = list(self.suspect_files)
        if self.issue_type:
            slots["issue_type"] = self.issue_type
        if self.exception_type:
            slots["exception_type"] = self.exception_type
        if self.exception_msg:
            slots["exception_msg"] = self.exception_msg.strip()[:300]
        if self.frames:
            slots["frames"] = [f.to_dict() for f in self.frames]
            top = self.top_frame
            if top:
                slots["top_frame"] = top.to_dict()
                slots["stack_line"] = top.line
        if self.has_traceback and self.stack_start >= 0:
            slots["stack_span"] = {"start": self.stack_start, "end": self.stack_end}
        return slots


def _norm_file(path: str) -> str:
    return path.replace("\\", "/").strip()


def _issue_type_from_exc(etype: str) -> str:
    return _ISSUE_TYPE_MAP.get(etype.lower(), etype.lower())


def _find_stack_region(text: str) -> tuple[int, int, str] | None:
    """Return (start, end, region_text) for the primary traceback block."""
    m = _TRACEBACK_START.search(text)
    if m:
        start = m.start()
        # Region runs until blank line after exception, or end of text.
        rest = text[start:]
        # Prefer cut after exception line + optional following non-indented prose
        exc = _EXCEPTION_LINE.search(rest)
        if exc:
            end_local = exc.end()
            # include a few indented context lines after File frames already in rest
            end = start + end_local
            return start, end, text[start:end]
        # fallback: from Traceback to end or next double newline after frames
        end = len(text)
        return start, end, text[start:end]

    # No header: first File "...", line N block through exception
    fm = _FILE_FRAME.search(text)
    if not fm:
        return None
    start = fm.start()
    rest = text[start:]
    exc = _EXCEPTION_LINE.search(rest)
    end = start + (exc.end() if exc else len(rest))
    # Only treat as stack if we see ≥1 File frame and an exception-ish line or ≥2 frames
    region = text[start:end]
    frames = list(_FILE_FRAME.finditer(region))
    if len(frames) >= 1 and (exc or len(frames) >= 2):
        return start, end, region
    return None


def _is_noise_frame(path: str) -> bool:
    """True for stdlib / venv / popular framework frames (not app suspects)."""
    low = path.replace("\\", "/").lower()
    noise_markers = (
        "site-packages/",
        "dist-packages/",
        "/lib/python",
        "\\lib\\python",
        "/usr/lib/",
        "/usr/local/lib/",
        "pyenv/versions/",
        "/venv/",
        "/.venv/",
        "/virtualenv/",
        "python39.zip",
        "python310.zip",
        "python311.zip",
        "python312.zip",
        "python313.zip",
        # test runner / plugin internals
        "/_pytest/",
        "/pytest/",
        "/pluggy/",
        # common ASGI/WSGI server internals when under site-packages already covered;
        # also bare relative imports from frozen
        "<frozen ",
    )
    if any(m in low for m in noise_markers):
        return True
    # Windows style ...\Python3x\Lib\...
    if re.search(r"/python3\d+/lib/", low):
        return True
    return False


def _is_absolute_foreign_path(path: str) -> bool:
    """True for absolute paths that usually come from another machine/venv paste."""
    norm = path.replace("\\", "/").strip()
    if re.match(r"^[A-Za-z]:/", norm):
        return True
    if norm.startswith(("/Users/", "/home/", "/private/var/", "/tmp/", "/var/folders/")):
        return True
    if norm.startswith("//"):
        return True
    return False


def relativize_suspect_path(path: str, *, repo_root: str | Path | None = None) -> str | None:
    """Map stack/Issue paths to project-relative; drop unmappable abs noise (E2).

    Does not hard-code repo names — uses existence under ``repo_root`` or a
    trailing ``pkg/.../file.py`` suffix heuristic.
    """
    if not path or not str(path).strip():
        return None
    norm = _norm_file(str(path))
    if _is_noise_frame(norm):
        return None
    for prefix in ("/app/", "/code/", "/workspace/", "/github/workspace/"):
        if norm.startswith(prefix):
            norm = norm[len(prefix) :]
            break
    root: Path | None = Path(repo_root) if repo_root else None
    if root is not None:
        try:
            # already relative and exists
            if not _is_absolute_foreign_path(norm) and (root / norm).is_file():
                return norm.replace("\\", "/")
            parts = [p for p in norm.replace("\\", "/").split("/") if p and p != ":"]
            # strip Windows drive letter segment
            if parts and len(parts[0]) == 1 and norm[1:3] in (":/", ":\\"):
                parts = parts[1:]
            elif re.match(r"^[A-Za-z]:", parts[0] if parts else ""):
                parts = parts[1:]
            for i in range(len(parts)):
                cand = "/".join(parts[i:])
                if cand and (root / cand).is_file():
                    return cand
        except OSError:
            pass
        # E2′: with repo_root, never keep paths that do not exist in the workspace
        return None
    if _is_absolute_foreign_path(norm):
        # trailing package-like relative: foo/bar/baz.py (no root to verify)
        m = re.search(r"((?:[\w.-]+/){1,}\w+\.py)$", norm)
        if m:
            return m.group(1)
        return None
    return norm


def _prefer_project_files(
    files: list[str], *, repo_root: str | Path | None = None
) -> list[str]:
    """If both absolute site paths and project-relative exist, keep non-noise only.

    Also strip leading workspace roots like /app/, /home/.../project/ when helpful
    for stable gold labels — keep path as pasted if already project-relative.
    Drops foreign absolute paths that cannot be relativized (E2).
    """
    cleaned: list[str] = []
    for f in files:
        rel = relativize_suspect_path(f, repo_root=repo_root)
        if not rel:
            continue
        if rel not in cleaned:
            cleaned.append(rel)
    return cleaned


def parse_stack(text: str, *, repo_root: str | Path | None = None) -> StackParseResult:
    """Extract traceback structure; empty result if no stack-like region."""
    raw = text or ""
    region_info = _find_stack_region(raw)
    result = StackParseResult()
    if not region_info:
        # soft: lone "at file.py:42" + exception name elsewhere still helpful
        at = _AT_FILE.search(raw)
        exc = _EXCEPTION_LINE.search(raw) or re.search(
            r"\b(TypeError|ImportError|ModuleNotFoundError|AttributeError|AssertionError)\b",
            raw,
        )
        if at and exc:
            path = _norm_file(at.group("file"))
            line = int(at.group("line"))
            mapped = relativize_suspect_path(path, repo_root=repo_root) or path
            if _is_noise_frame(mapped) or (
                _is_absolute_foreign_path(path) and mapped == path
            ):
                mapped_list = _prefer_project_files([path], repo_root=repo_root)
                mapped = mapped_list[0] if mapped_list else ""
            result.has_traceback = True
            result.frames = [StackFrame(file=mapped or path, line=line)]
            result.suspect_files = [mapped] if mapped else []
            if hasattr(exc, "lastindex") and exc.lastindex:  # EXCEPTION_LINE
                et = exc.group("etype") if "etype" in exc.groupdict() else exc.group(1)
                result.exception_type = et
                result.issue_type = _issue_type_from_exc(et)
                if "msg" in exc.groupdict():
                    result.exception_msg = exc.group("msg") or ""
            else:
                result.exception_type = exc.group(1)
                result.issue_type = _issue_type_from_exc(exc.group(1))
            result.stack_text = raw[at.start() : at.end()]
            return result
        return result

    start, end, region = region_info
    result.has_traceback = True
    result.stack_start = start
    result.stack_end = end
    result.stack_text = region

    frames: list[StackFrame] = []
    for fm in _FILE_FRAME.finditer(region):
        frames.append(
            StackFrame(
                file=_norm_file(fm.group("file")),
                line=int(fm.group("line")),
                func=(fm.group("func") or "").strip(),
            )
        )
    result.frames = frames

    exc = _EXCEPTION_LINE.search(region)
    if exc:
        result.exception_type = exc.group("etype")
        result.exception_msg = exc.group("msg") or ""
        result.issue_type = _issue_type_from_exc(result.exception_type)

    files: list[str] = []
    for fr in frames:
        if _is_noise_frame(fr.file):
            continue
        if fr.file not in files:
            files.append(fr.file)
    result.suspect_files = _prefer_project_files(files, repo_root=repo_root)
    # rewrite frame paths when relativized
    mapped = {f: relativize_suspect_path(f, repo_root=repo_root) for f in files}
    new_frames: list[StackFrame] = []
    for fr in result.frames:
        mf = mapped.get(fr.file) or relativize_suspect_path(fr.file, repo_root=repo_root)
        if mf:
            new_frames.append(StackFrame(file=mf, line=fr.line, func=fr.func))
        elif not _is_noise_frame(fr.file) and not _is_absolute_foreign_path(fr.file):
            new_frames.append(fr)
    if new_frames:
        result.frames = new_frames
    return result


def extract_issue_slots(
    text: str, *, repo_root: str | Path | None = None
) -> dict[str, Any]:
    """Slots for intent routing: stack-first, then safe fallbacks (no fence noise)."""
    parsed = parse_stack(text, repo_root=repo_root)
    if parsed.has_traceback:
        return parsed.to_slots()

    slots: dict[str, Any] = {}
    # Strip fenced code before bare .py scraping to reduce paste noise
    scrubbed = _CODE_FENCE.sub("\n", text or "")

    files: list[str] = []
    for m in re.finditer(r'File\s+"([^"]+)"', scrubbed):
        name = _norm_file(m.group(1))
        if name not in files:
            files.append(name)
    for m in _AT_FILE.finditer(scrubbed):
        name = _norm_file(m.group("file"))
        if name not in files:
            files.append(name)
    cand = _CANDIDATE_FILES.search(scrubbed)
    if cand:
        for raw in cand.group("body").split(","):
            name = _norm_file(raw.strip())
            if name and name not in files:
                files.append(name)

    # Short utterances: allow bare .py; skip source-like lines (def/class/=).
    if not files and len(scrubbed) < 400:
        source_like = bool(
            re.search(r"(?m)^(def |class |import |from )", scrubbed)
            or scrubbed.count("=") >= 3
        )
        if not source_like:
            for m in _BARE_PY.finditer(scrubbed):
                name = _norm_file(m.group(1))
                if name not in files:
                    files.append(name)

    if files:
        slots["suspect_files"] = _prefer_project_files(files, repo_root=repo_root)

    m = re.search(
        r"\b(TypeError|ImportError|ModuleNotFoundError|AttributeError|"
        r"logic\s*error|test\s*failure|config\s*error)\b",
        scrubbed,
        re.I,
    )
    if m:
        raw = m.group(0).lower().replace(" ", "_")
        slots["issue_type"] = _ISSUE_TYPE_MAP.get(raw, raw)

    return slots


def has_stack_signal(text: str) -> bool:
    """True if text contains a parseable traceback or strong stack hint."""
    if parse_stack(text).has_traceback:
        return True
    raw = text or ""
    return bool(
        re.search(r"(?i)traceback\s*\(most recent", raw)
        or _FILE_FRAME.search(raw)
        # pytest / CI failure paste without classic Traceback header
        or re.search(r"(?im)^={5,}\s*FAILURES\s*={5,}", raw)
        or re.search(r"(?im)^E\s+AssertionError:", raw)
        or re.search(r"(?i)FAILED\s+[\w./\\:-]+::\w+", raw)
    )
