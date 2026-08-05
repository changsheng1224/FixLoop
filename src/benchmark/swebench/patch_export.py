"""从 repair 结果导出 unified patch（E1/E12/E13）。"""

from __future__ import annotations

import difflib
import re
from pathlib import Path

from src.eval.runner import should_include_in_eval_diff

# 防止脏工作树 / 整仓 CRLF 噪声把 predictions 撑爆（E12）
MAX_EXPORT_FILES = 32
MAX_EXPORT_BYTES = 256_000


def normalize_patch_lf(text: str) -> str:
    """统一为 LF，避免 Windows CR 导致官方 harness ``different line endings``（E1）。"""
    if not text:
        return ""
    return text.replace("\r\n", "\n").replace("\r", "")


def looks_like_unified_diff(text: str) -> bool:
    """至少含一个带路径的 unified 文件头（E13）。"""
    if not (text or "").strip():
        return False
    return bool(
        re.search(r"(?m)^--- [^\n]+$", text)
        and re.search(r"(?m)^\+\+\+ [^\n]+$", text)
    )


def count_diff_files(text: str) -> int:
    return len(re.findall(r"(?m)^\+\+\+ ", text or ""))


def _ensure_nl(lines: list[str]) -> list[str]:
    if not lines:
        return []
    out = list(lines)
    if out and not out[-1].endswith("\n"):
        out[-1] = out[-1] + "\n"
    return out


def lines_to_unified_diff(file_path: str, original: str, patched: str) -> str:
    """由 original/patched 文本生成带路径的 unified diff（E13）。"""
    rel = (file_path or "unknown").replace("\\", "/").lstrip("./")
    old = _ensure_nl((original or "").splitlines(keepends=True))
    new = _ensure_nl((patched or "").splitlines(keepends=True))
    if old == new:
        return ""
    return "".join(
        difflib.unified_diff(
            old,
            new,
            fromfile=f"a/{rel}",
            tofile=f"b/{rel}",
        )
    )


def _normalize_existing_diff(diff: str, file_path: str = "") -> str:
    """已有 diff 缺文件头时，尽量补全或判定无效。"""
    text = normalize_patch_lf(diff)
    if looks_like_unified_diff(text):
        return text if text.endswith("\n") else text + "\n"
    # 仅 +/- 行：若有 file_path + 无法还原上下文，则丢弃（避免假 nonempty）
    if file_path and (text.lstrip().startswith(("+", "-", "@@"))):
        # 无法从残片可靠还原 → 空，交给 original/patched 路径
        return ""
    return ""


def candidate_to_unified(patch) -> str:
    """单个 CandidatePatch → unified diff。"""
    file_path = getattr(patch, "file_path", "") or ""
    if hasattr(patch, "to_dict"):
        d = patch.to_dict()
    elif isinstance(patch, dict):
        d = patch
        file_path = file_path or str(d.get("file_path") or "")
    else:
        d = {}

    raw_diff = str(
        getattr(patch, "diff", None)
        or d.get("diff")
        or d.get("unified_diff")
        or ""
    )
    if raw_diff.strip():
        norm = _normalize_existing_diff(raw_diff, file_path)
        if norm:
            return norm

    original = str(getattr(patch, "original_lines", None) or d.get("original_lines") or "")
    patched = str(getattr(patch, "patched_lines", None) or d.get("patched_lines") or "")
    if file_path and (original or patched):
        return lines_to_unified_diff(file_path, original, patched)
    return ""


def patch_paths_from_state(state) -> list[str]:
    paths: list[str] = []
    for p in getattr(state, "candidate_patches", None) or []:
        fp = getattr(p, "file_path", None)
        if fp is None and isinstance(p, dict):
            fp = p.get("file_path")
        if fp:
            paths.append(str(fp).replace("\\", "/").lstrip("./"))
    return paths


def suspect_paths_from_state(state) -> list[str]:
    """定位嫌疑 + plan + allowed_extra，供无 candidate 时的 scoped 导出。"""
    paths: list[str] = []
    seen: set[str] = set()

    def _add(raw: object) -> None:
        fp = str(raw or "").replace("\\", "/").lstrip("./")
        if fp and fp not in seen:
            seen.add(fp)
            paths.append(fp)

    for s in getattr(state, "suspect_locations", None) or []:
        _add(getattr(s, "file_path", None))
    plan = getattr(state, "repair_plan", None)
    if plan is not None:
        for f in getattr(plan, "suspect_files", None) or []:
            _add(f)
    timings = getattr(state, "node_timings", None) or {}
    for f in timings.get("allowed_patch_extra") or []:
        _add(f)
    return paths


def patch_from_state(state, *, fallback_diff: str = "") -> str:
    """仅从 candidate_patches 拼 unified；无合法候选则不用整仓 fallback（E12）。"""
    patches = getattr(state, "candidate_patches", None) or []
    chunks: list[str] = []
    for p in patches:
        u = candidate_to_unified(p)
        if u.strip():
            chunks.append(u if u.endswith("\n") else u + "\n")
    if chunks:
        return gate_export_size(normalize_patch_lf("".join(chunks)))
    # 显式禁止把「脏仓整 diff」当默认（E12）；fallback 仅当调用方传入且已门禁
    if fallback_diff and looks_like_unified_diff(fallback_diff):
        return gate_export_size(normalize_patch_lf(fallback_diff))
    return ""


def gate_export_size(text: str) -> str:
    """超文件数/字节则视为无效导出（E12）。"""
    if not text.strip():
        return ""
    if count_diff_files(text) > MAX_EXPORT_FILES:
        return ""
    if len(text.encode("utf-8")) > MAX_EXPORT_BYTES:
        return ""
    if not looks_like_unified_diff(text):
        return ""
    return text


def _read_text_safe(path: Path) -> list[str] | None:
    """文本文件按行读取；二进制返回 None。"""
    try:
        raw = path.read_bytes()
    except OSError:
        return None
    if b"\x00" in raw[:8192]:
        return None
    try:
        return raw.decode("utf-8").splitlines(keepends=True)
    except UnicodeDecodeError:
        return None


def collect_repo_diff_safe(
    original: Path,
    modified: Path,
    *,
    only_files: set[str] | list[str] | None = None,
    max_files: int = MAX_EXPORT_FILES,
    max_bytes: int = MAX_EXPORT_BYTES,
) -> str:
    """对比两个 repo；可限制文件集；超限返回空（E12）。"""
    parts: list[str] = []
    allow = None
    if only_files is not None:
        allow = {str(p).replace("\\", "/").lstrip("./") for p in only_files}

    all_files: set[str] = set()
    for root in (original, modified):
        for path in root.rglob("*"):
            if not path.is_file():
                continue
            rel = path.relative_to(root).as_posix()
            if allow is not None and rel not in allow:
                continue
            if should_include_in_eval_diff(rel):
                all_files.add(rel)

    changed = 0
    for rel in sorted(all_files):
        o = original / rel
        m = modified / rel
        old = _read_text_safe(o) if o.is_file() else []
        new = _read_text_safe(m) if m.is_file() else []
        if old is None or new is None:
            continue
        if old == new:
            continue
        changed += 1
        if changed > max_files:
            return ""
        parts.append(
            "".join(
                difflib.unified_diff(
                    old or [],
                    new or [],
                    fromfile=f"a/{rel}",
                    tofile=f"b/{rel}",
                )
            )
        )
        blob = "".join(parts)
        if len(blob.encode("utf-8")) > max_bytes:
            return ""
    return gate_export_size(normalize_patch_lf("".join(parts)))


def export_model_patch(
    *,
    state,
    original_repo: Path | None = None,
    modified_repo: Path | None = None,
) -> str:
    """导出 harness 可用的 model_patch。

    优先级：
    1. candidate_patches → unified（E13）
    2. 候选路径 scoped repo diff（E12）
    3. suspect/plan 路径 scoped repo diff（P1：verify 失败仍可导出非空）
    4. 否则空串（禁止整仓脏 diff）
    """
    text = patch_from_state(state)
    if text.strip():
        return text

    paths = patch_paths_from_state(state)
    if paths and original_repo and modified_repo:
        scoped = collect_repo_diff_safe(
            Path(original_repo),
            Path(modified_repo),
            only_files=set(paths),
        )
        if scoped.strip():
            return scoped

    suspects = suspect_paths_from_state(state)
    if suspects and original_repo and modified_repo:
        scoped = collect_repo_diff_safe(
            Path(original_repo),
            Path(modified_repo),
            only_files=set(suspects),
        )
        if scoped.strip():
            return scoped
    return ""
