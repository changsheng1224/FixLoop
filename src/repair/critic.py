"""提交前轻量评审（Critic）：默认 rules_first，不替代 Verifier。

故意保持极简：空 / 仅测。越锁交给 edit_lock；补丁质量交给模型与 Verifier。
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass

from src.state import CandidatePatch

__all__ = ["CriticVerdict", "review_patch", "resolve_critic_mode"]


@dataclass
class CriticVerdict:
    accepted: bool
    reason: str
    mode: str
    skipped: bool = False


def resolve_critic_mode(env: dict[str, str] | None = None) -> str:
    """返回 rules_first | llm | off。

    默认 ``rules_first``；可用 ``FIXLOOP_CRITIC_MODE`` 覆盖。
    """
    e = env if env is not None else os.environ
    raw = e.get("FIXLOOP_CRITIC")
    if raw is not None and raw.strip() != "":
        if raw.strip().lower() in ("0", "false", "off", "no"):
            return "off"
        mode = (e.get("FIXLOOP_CRITIC_MODE") or "rules_first").strip().lower()
        if mode in ("llm", "rules_first", "off"):
            return mode
        return "rules_first"

    mode = (e.get("FIXLOOP_CRITIC_MODE") or "rules_first").strip().lower()
    return mode if mode in ("llm", "rules_first", "off") else "rules_first"


def _norm_path(p: str) -> str:
    return (p or "").replace("\\", "/").lstrip("./")


_TEST_PATH_RE = re.compile(
    r"(^|/)(tests?/|test_)|(_test\.py$)|(/conftest\.py$)",
    re.IGNORECASE,
)


def _is_test_path(path: str) -> bool:
    return bool(_TEST_PATH_RE.search(_norm_path(path)))


def _paths_from_patches(patches: list[CandidatePatch]) -> list[str]:
    out: list[str] = []
    for p in patches or []:
        fp = _norm_path(getattr(p, "file_path", "") or "")
        if fp:
            out.append(fp)
    return out


def _has_nonempty_change(patches: list[CandidatePatch]) -> bool:
    for p in patches or []:
        diff = (getattr(p, "diff", "") or "").strip()
        if diff:
            return True
        orig = getattr(p, "original_lines", None)
        patched = getattr(p, "patched_lines", None)
        if orig is not None and patched is not None and orig != patched:
            return True
    return False


def review_patch(
    patches: list[CandidatePatch] | None,
    *,
    allowed_edit: set[str] | frozenset[str] | list[str] | None = None,
    mode: str | None = None,
    reject_tests_only: bool = True,
) -> CriticVerdict:
    """评审候选补丁。

    rules_first：空 / 仅改测试 → reject。越锁交给写时 edit_lock；质量交给模型与 Verifier。
    """
    resolved = (mode or resolve_critic_mode()).strip().lower()
    if resolved == "off":
        return CriticVerdict(accepted=True, reason="critic_off", mode="off")

    if resolved == "llm":
        return CriticVerdict(
            accepted=True,
            reason="critic_skipped",
            mode="llm",
            skipped=True,
        )

    patches = list(patches or [])
    if not patches or not _has_nonempty_change(patches):
        return CriticVerdict(
            accepted=False,
            reason="empty_patch",
            mode="rules_first",
        )

    paths = _paths_from_patches(patches)
    # allowed_edit 仅作可选提示；越锁不在此拒绝（避免与 edit_lock 双闸）
    _ = {_norm_path(p) for p in (allowed_edit or []) if p}

    if reject_tests_only and paths and all(_is_test_path(p) for p in paths):
        return CriticVerdict(
            accepted=False,
            reason="tests_only",
            mode="rules_first",
        )

    return CriticVerdict(accepted=True, reason="ok", mode="rules_first")
