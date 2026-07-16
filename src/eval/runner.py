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

from src.eval.case_io import DEFAULT_CASES_DIR, build_case_issue, load_case_metadata
from src.eval.models import CaseResult, EvalReport
from src.eval.patch_utils import apply_unified_patch
from src.repair.termination import introduced_regression, regression_detected

# Agent 运行时会在 repo 内写入的目录，不计入评测 patch diff
EVAL_DIFF_SKIP_DIRS = frozenset({".agent", ".pytest_cache", "__pycache__", ".git"})


def _copy_case_repo(src: Path, dst: Path) -> None:
    """Copy an eval case repo while skipping generated/cache directories."""
    shutil.copytree(
        src,
        dst,
        ignore=shutil.ignore_patterns(*EVAL_DIFF_SKIP_DIRS),
    )


def should_include_in_eval_diff(rel_path: str) -> bool:
    """评测 diff 只统计项目源码变更，排除 Agent/pytest 运行时产物。"""
    parts = Path(rel_path).parts
    if any(part in EVAL_DIFF_SKIP_DIRS for part in parts):
        return False
    if parts and parts[0].startswith("."):
        return False
    return True


def run_pytest(repo: Path) -> tuple[int, str]:
    """在 repo 根目录运行 pytest -q，返回 (exit_code, combined_output)。"""
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
    """统计 unified diff 中新增行数（不含 +++ 头）。"""
    return sum(
        1 for line in patch_text.splitlines() if line.startswith("+") and not line.startswith("+++")
    )


def extract_agent_timings(node_timings: dict | None) -> dict:
    """从 RepairState.node_timings 提取评测关心的耗时与性能字段。"""
    if not node_timings:
        return {}
    from src.repair.timing_schema import PHASES, get_phase_ms, phase_ms_key

    result: dict = {}
    for key in ("parse_issue_ms", "localize_retrieve_ms", "baseline_ms"):
        if key in node_timings:
            result[key] = node_timings[key]
    parallel = node_timings.get("parallel_wall_ms") or {}
    if "localize_retrieve_ms" in parallel and "localize_retrieve_ms" not in result:
        result["localize_retrieve_ms"] = parallel["localize_retrieve_ms"]
    for phase in PHASES:
        canon = phase_ms_key(phase)
        ms = get_phase_ms(node_timings, phase)
        if ms or canon in (node_timings.get("phases") or {}):
            result[canon] = ms
    legacy_keys = (
        "localizer_ms",
        "retriever_ms",
        "patcher_ms",
        "verifier_ms",
    )
    for key in legacy_keys:
        if key in node_timings:
            result[key] = node_timings[key]

    # 性能矩阵字段（V1.4-Bonus5c）
    token_usage = node_timings.get("token_usage") or {}
    if isinstance(token_usage, dict):
        sections = token_usage.get("sections") or token_usage.get("token_usage") or {}
        result["context_tokens"] = (
            sum(int(v) for v in sections.values()) if isinstance(sections, dict) else 0
        )
        result["cache_hit_rate"] = float(token_usage.get("cache_hit_rate", 0) or 0)
    result["total_tool_steps"] = int(
        node_timings.get("total_tool_steps", 0) or node_timings.get("tool_steps", 0) or 0
    )
    # p50 ttft 从 agent_reports 延迟计算（此处存原始值）
    ttft_vals = node_timings.get("ttft_ms")
    if ttft_vals is None:
        ttft_by_agent = node_timings.get("latency_by_agent") or {}
        ttft_vals = [
            int(v.get("ttft_ms", 0) or 0)
            for v in ttft_by_agent.values()
            if isinstance(v, dict) and v.get("ttft_ms")
        ]
    if ttft_vals:
        result["ttft_values"] = list(ttft_vals) if isinstance(ttft_vals, list) else [ttft_vals]
    return result


class EvalRunner:
    """评测运行器。"""

    CHECKPOINT_FILENAME = ".checkpoint.json"

    def __init__(
        self,
        orchestrator_factory: Callable[[str], object],
        cases_dir: str | Path | None = None,
        output_dir: str | Path | None = None,
        skip_verify: bool = False,
        judge_client=None,
    ):
        self.orchestrator_factory = orchestrator_factory
        self.cases_dir = Path(cases_dir or DEFAULT_CASES_DIR)
        self.output_dir = Path(output_dir or Path.cwd() / "eval_results")
        self.skip_verify = skip_verify
        self.judge_client = judge_client

    @property
    def _checkpoint_path(self) -> Path:
        return self.output_dir / self.CHECKPOINT_FILENAME

    def _load_checkpoint(self) -> set[tuple[str, str, int]]:
        """加载已完成的 (case_id, variant, rep) 集合。

        文件不存在或损坏时返回空集合。
        """
        cp = self._checkpoint_path
        if not cp.is_file():
            return set()
        try:
            entries = json.loads(cp.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return set()
        result = set()
        for e in entries:
            result.add(
                (
                    str(e.get("case_id", "")),
                    str(e.get("variant", "")),
                    int(e.get("rep", 0)),
                )
            )
        return result

    def _save_checkpoint_entry(self, case_id: str, variant: str = "", rep: int = 0) -> None:
        """追加一条 checkpoint 记录（逐行安全追加）。"""
        cp = self._checkpoint_path
        cp.parent.mkdir(parents=True, exist_ok=True)
        entries = []
        if cp.is_file():
            try:
                entries = json.loads(cp.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                entries = []
        entries.append({"case_id": case_id, "variant": variant, "rep": rep})
        tmp = cp.with_suffix(".tmp")
        tmp.write_text(json.dumps(entries, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(cp)

    def _clear_checkpoint(self) -> None:
        """删除 checkpoint 文件（全部完成时调用）。"""
        cp = self._checkpoint_path
        if cp.is_file():
            cp.unlink()

    def _is_fake_mode(self) -> bool:
        if hasattr(self, "_cached_fake_mode"):
            return self._cached_fake_mode
        from src.eval.fake_runner import FakePatchOrchestrator

        try:
            self._cached_fake_mode = isinstance(
                self.orchestrator_factory("."), FakePatchOrchestrator
            )
        except Exception:
            self._cached_fake_mode = False
        return self._cached_fake_mode

    def list_cases(self) -> list[str]:
        """列出 cases_dir 下 case_NNN 目录名。"""
        return sorted(
            p.name
            for p in self.cases_dir.iterdir()
            if p.is_dir() and re.match(r"case_[\w]+$", p.name)
        )

    def run_all(
        self,
        case_ids: list[str] | None = None,
        report_path: Path | None = None,
        *,
        resume: bool = False,
        variant: str = "",
        rep: int = 0,
        repetitions: int = 1,
    ) -> EvalReport:
        """运行多个 Case，写入 eval_report.json 并返回聚合报告。

        Args:
            case_ids: 待运行的 case ID 列表。None 时运行全部。
            report_path: 报告输出路径。None 时自动生成。
            resume: 启用断点续跑（跳过 checkpoint 中已完成的 case）。
            variant: 变体名（预留，供 ablation/Pass@k 使用）。
            rep: 重复序号（预留，供 Pass@k 使用）。
            repetitions: 每个 case 重复运行次数（Pass@k）。默认 1。
        """
        ids = case_ids or self.list_cases()
        completed = self._load_checkpoint() if resume else set()

        results: list[CaseResult] = []
        for case_id in ids:
            for run_idx in range(repetitions):
                if resume and (case_id, variant, run_idx) in completed:
                    continue
                result = self.run_case(case_id)
                result.run_index = run_idx
                results.append(result)
                if resume:
                    self._save_checkpoint_entry(
                        case_id,
                        variant=variant,
                        rep=run_idx,
                    )

        if resume and not results:
            print("[eval] 所有 case 已完成", file=sys.stderr)
            return build_eval_report([])

        report = build_eval_report(results)
        out = report_path or (self.output_dir / "eval_report.json")
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(report.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8")

        if resume:
            self._clear_checkpoint()
        return report

    def run_case(self, case_id: str) -> CaseResult:
        """运行单个 Case：复制 repo → repair → pytest → 记录 diff 与指标。"""
        case_dir = self.cases_dir / case_id
        if not case_dir.is_dir():
            return CaseResult(case_id=case_id, error=f"unknown case: {case_id}")

        meta = load_case_metadata(case_dir)
        language = str(meta.get("language", "python")).lower()
        is_fake = self._is_fake_mode()
        issue = build_case_issue(case_dir, metadata=meta)
        minimal_lines = _read_min_lines(case_dir)

        with tempfile.TemporaryDirectory(prefix=f"fixloop_eval_{case_id}_") as tmp:
            tmp_repo = Path(tmp) / "repo"
            _copy_case_repo(case_dir / "repo", tmp_repo)

            pre_code, pre_out = (
                (0, "") if (is_fake and language != "python") else run_pytest(tmp_repo)
            )

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

            post_code, post_out = (
                (0, "") if (is_fake and language != "python") else run_pytest(tmp_repo)
            )
            fixed = post_code == 0

            original_snapshot = Path(tmp) / "original"
            _copy_case_repo(case_dir / "repo", original_snapshot)
            actual_patch = collect_repo_diff(original_snapshot, tmp_repo)
            actual_lines = count_changed_lines(actual_patch)

            introduced_regression_flag = regression_detected(pre_code, post_code)
            if state and introduced_regression(state):
                introduced_regression_flag = True

            retry_count = state.retry_count if state else 0
            status = state.status if state else ""
            failure_tags = list(state.failure_tags) if state else []
            total_tokens = 0
            token_usage: dict = {}
            permission_denied_by_tool: dict = {}
            if state and getattr(state, "node_timings", None):
                node_timings = state.node_timings
                token_usage = node_timings.get("token_usage") or {}
                permission_denied_by_tool = dict(
                    node_timings.get("permission_denied_by_tool") or {}
                )
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

            status = str(getattr(state, "status", "") or "").strip()
            if not status:
                status = "fixed" if fixed else "failed"
            if status == "pending":
                status = "fixed" if fixed else "failed"

            from src.eval.skill_metrics import skill_result_fields_from_plan

            plan = getattr(state, "repair_plan", None) if state else None
            expected_skill, matched_skill, skill_match, skill_labeled = (
                skill_result_fields_from_plan(plan, meta)
            )

            judge_score, judge_reason = 0, ""
            if self.judge_client and actual_patch:
                judge_score, judge_reason = self.judge_client.evaluate(
                    issue,
                    actual_patch,
                )

            # 计算 patch equivalence（vs expected_patch.diff）
            equivalence = "none"
            if fixed:
                try:
                    from src.eval.patch_utils import patch_equivalence

                    expected = (case_dir / "expected_patch.diff").read_text(encoding="utf-8")
                    equivalence = patch_equivalence(actual_patch, expected)
                except Exception:
                    pass

            return CaseResult(
                case_id=case_id,
                issue_type=str(meta.get("issue_type", "")),
                difficulty=str(meta.get("difficulty", "")),
                expected_skill=expected_skill,
                matched_skill=matched_skill,
                skill_match=skill_match,
                skill_labeled=skill_labeled,
                fixed=fixed,
                retry_count=retry_count,
                actual_patch=actual_patch,
                actual_lines=actual_lines,
                minimal_lines=minimal_lines,
                duration_ms=duration_ms,
                agent_timings=extract_agent_timings(getattr(state, "node_timings", None)),
                error=error,
                introduced_regression=introduced_regression_flag,
                status=status,
                failure_tags=failure_tags,
                total_tokens=total_tokens,
                token_usage=token_usage if isinstance(token_usage, dict) else {},
                permission_denied_by_tool=permission_denied_by_tool,
                equivalence=equivalence,
                judge_score=judge_score,
                judge_reason=judge_reason,
            )


def build_eval_report(results: list[CaseResult]) -> EvalReport:
    """将 CaseResult 列表聚合为 EvalReport（含 summary / by_type 等）。"""
    from src.eval.metrics import compute_metrics

    return compute_metrics(results)


def _read_min_lines(case_dir: Path) -> int:
    min_lines_path = case_dir / "min_lines.txt"
    if not min_lines_path.is_file():
        return 0
    text = min_lines_path.read_text(encoding="utf-8").strip()
    if text.upper() == "TBD":
        return 0
    return int(text)


def apply_expected_patch_to_repo(repo: Path, case_dir: Path) -> None:
    """测试/基线用：将 expected_patch 应用到 repo 副本。"""
    patch = (case_dir / "expected_patch.diff").read_text(encoding="utf-8")
    apply_unified_patch(repo, patch)
