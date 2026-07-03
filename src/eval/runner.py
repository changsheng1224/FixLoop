"""EvalRunner：遍历评测 Case，调用 Orchestrator 并 pytest 验证。"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import sys
import tempfile
import time
from collections.abc import Callable
from pathlib import Path

import yaml

from src.eval.models import CaseResult, EvalReport
from src.eval.patch_utils import apply_unified_patch

DEFAULT_CASES_DIR = Path(__file__).resolve().parent / "cases"

# Agent 运行时会在 repo 内写入的目录，不计入评测 patch diff
EVAL_DIFF_SKIP_DIRS = frozenset({".agent", ".pytest_cache", "__pycache__", ".git"})


def should_include_in_eval_diff(rel_path: str) -> bool:
    """评测 diff 只统计项目源码变更，排除 Agent/pytest 运行时产物。"""
    parts = Path(rel_path).parts
    if any(part in EVAL_DIFF_SKIP_DIRS for part in parts):
        return False
    if parts and parts[0].startswith("."):
        return False
    return True


def load_case_metadata(case_dir: Path) -> dict:
    meta_path = case_dir / "metadata.yaml"
    if not meta_path.is_file():
        return {}
    return yaml.safe_load(meta_path.read_text(encoding="utf-8")) or {}


def run_pytest(repo: Path) -> tuple[int, str]:
    proc = subprocess.run(
        [sys.executable, "-m", "pytest", "-q"],
        cwd=repo,
        capture_output=True,
        text=True,
    )
    out = (proc.stdout or "") + (proc.stderr or "")
    return proc.returncode, out


def collect_repo_diff(original: Path, modified: Path) -> str:
    """对比两个 repo 目录，生成 unified diff 文本。"""
    import difflib

    parts: list[str] = []
    all_files = set()
    for root in (original, modified):
        for path in root.rglob("*"):
            if not path.is_file():
                continue
            rel = path.relative_to(root).as_posix()
            if should_include_in_eval_diff(rel):
                all_files.add(rel)
    for rel in sorted(all_files):
        o = original / rel
        m = modified / rel
        old = o.read_text(encoding="utf-8").splitlines(keepends=True) if o.is_file() else []
        new = m.read_text(encoding="utf-8").splitlines(keepends=True) if m.is_file() else []
        if old != new:
            parts.append(
                "".join(
                    difflib.unified_diff(
                        old,
                        new,
                        fromfile=f"a/{rel}",
                        tofile=f"b/{rel}",
                    )
                )
            )
    return "\n".join(p.strip("\n") for p in parts if p.strip())


def count_changed_lines(patch_text: str) -> int:
    return sum(1 for line in patch_text.splitlines() if line.startswith("+") and not line.startswith("+++"))


def extract_agent_timings(node_timings: dict | None) -> dict:
    if not node_timings:
        return {}
    keys = (
        "parse_issue_ms",
        "localize_retrieve_ms",
        "localizer_ms",
        "retriever_ms",
        "patcher_ms",
        "verifier_ms",
        "baseline_ms",
    )
    return {k: node_timings[k] for k in keys if k in node_timings}


class EvalRunner:
    """评测运行器。"""

    def __init__(
        self,
        orchestrator_factory: Callable[[str], object],
        cases_dir: str | Path | None = None,
        output_dir: str | Path | None = None,
        skip_verify: bool = False,
    ):
        self.orchestrator_factory = orchestrator_factory
        self.cases_dir = Path(cases_dir or DEFAULT_CASES_DIR)
        self.output_dir = Path(output_dir or Path.cwd() / "eval_results")
        self.skip_verify = skip_verify

    def list_cases(self) -> list[str]:
        return sorted(
            p.name
            for p in self.cases_dir.iterdir()
            if p.is_dir() and re.match(r"case_\d{3}$", p.name)
        )

    def run_all(self, case_ids: list[str] | None = None, report_path: Path | None = None) -> EvalReport:
        ids = case_ids or self.list_cases()
        results = [self.run_case(case_id) for case_id in ids]
        report = build_eval_report(results)
        out = report_path or (self.output_dir / "eval_report.json")
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(report.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8")
        return report

    def run_case(self, case_id: str) -> CaseResult:
        case_dir = self.cases_dir / case_id
        if not case_dir.is_dir():
            return CaseResult(case_id=case_id, error=f"unknown case: {case_id}")

        meta = load_case_metadata(case_dir)
        issue = (case_dir / "issue.txt").read_text(encoding="utf-8").strip()
        source_files = meta.get("source_files") or []
        if source_files:
            issue = f"{issue}\n\nCandidate source files: {', '.join(source_files)}"
        minimal_lines = _read_min_lines(case_dir)

        with tempfile.TemporaryDirectory(prefix=f"fixloop_eval_{case_id}_") as tmp:
            tmp_repo = Path(tmp) / "repo"
            shutil.copytree(case_dir / "repo", tmp_repo)

            pre_code, _pre_out = run_pytest(tmp_repo)
            pre_passing = pre_code == 0

            t0 = time.time()
            error = ""
            state = None
            try:
                orch = self.orchestrator_factory(str(tmp_repo.resolve()))
                if hasattr(orch, "_case_id"):
                    orch._case_id = case_id
                state = orch.repair(issue)
            except Exception as exc:
                error = str(exc)
            duration_ms = int((time.time() - t0) * 1000)

            post_code, post_out = run_pytest(tmp_repo)
            fixed = post_code == 0

            original_snapshot = Path(tmp) / "original"
            shutil.copytree(case_dir / "repo", original_snapshot)
            actual_patch = collect_repo_diff(original_snapshot, tmp_repo)
            actual_lines = count_changed_lines(actual_patch)

            introduced_regression = False
            if not pre_passing and not fixed and post_code != pre_code:
                introduced_regression = post_code != 0 and pre_code != 0

            retry_count = getattr(state, "retry_count", 0) if state else 0
            status = getattr(state, "status", "") if state else ""
            total_tokens = 0
            token_usage: dict = {}
            if state and getattr(state, "node_timings", None):
                node_timings = state.node_timings
                token_usage = node_timings.get("token_usage") or {}
                if isinstance(token_usage, dict):
                    total_tokens = int(
                        node_timings.get("total_tokens", 0) or token_usage.get("total_tokens", 0)
                    )
                else:
                    token_usage = {}
            if not error and state and getattr(state, "agent_errors", None):
                errs = state.agent_errors
                if errs:
                    error = "; ".join(f"{k}: {v}" for k, v in errs.items())

            return CaseResult(
                case_id=case_id,
                issue_type=str(meta.get("issue_type", "")),
                difficulty=str(meta.get("difficulty", "")),
                fixed=fixed,
                retry_count=retry_count,
                actual_patch=actual_patch,
                actual_lines=actual_lines,
                minimal_lines=minimal_lines,
                duration_ms=duration_ms,
                agent_timings=extract_agent_timings(getattr(state, "node_timings", None)),
                error=error,
                introduced_regression=introduced_regression,
                status=status,
                total_tokens=total_tokens,
                token_usage=token_usage if isinstance(token_usage, dict) else {},
            )


def build_eval_report(results: list[CaseResult]) -> EvalReport:
    from src.eval.metrics import compute_metrics

    return compute_metrics(results)


def _read_min_lines(case_dir: Path) -> int:
    text = (case_dir / "min_lines.txt").read_text(encoding="utf-8").strip()
    if text.upper() == "TBD":
        return 0
    return int(text)


def apply_expected_patch_to_repo(repo: Path, case_dir: Path) -> None:
    """测试/基线用：将 expected_patch 应用到 repo 副本。"""
    patch = (case_dir / "expected_patch.diff").read_text(encoding="utf-8")
    apply_unified_patch(repo, patch)
