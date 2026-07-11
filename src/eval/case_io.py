"""Eval case I/O helpers (metadata, issue text)."""

from __future__ import annotations

from pathlib import Path

import yaml

DEFAULT_CASES_DIR = Path(__file__).resolve().parent / "cases"


def load_case_metadata(case_dir: Path) -> dict:
    """读取 Case 目录下的 metadata.yaml，缺失时返回空 dict。"""
    meta_path = case_dir / "metadata.yaml"
    if not meta_path.is_file():
        return {}
    return yaml.safe_load(meta_path.read_text(encoding="utf-8")) or {}


def build_case_issue(case_dir: Path, *, metadata: dict | None = None) -> str:
    """构建与 EvalRunner.repair 一致的 issue 文本。"""
    meta = metadata if metadata is not None else load_case_metadata(case_dir)
    issue = (case_dir / "issue.txt").read_text(encoding="utf-8").strip()
    source_files = meta.get("source_files") or []
    if source_files:
        issue = f"{issue}\n\nCandidate source files: {', '.join(source_files)}"
    return issue
