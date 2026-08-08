"""EvalRunner：遍历评测 Case，调用 Orchestrator 并 pytest 验证。"""

from __future__ import annotations

import hashlib
import json
import platform
import re
import shutil
import subprocess
import sys
import tempfile
import time
import uuid
from collections.abc import Callable
from pathlib import Path

from src.eval.case_io import DEFAULT_CASES_DIR, build_case_issue, load_case_metadata
from src.eval.contracts import (
    EVAL_CONTRACT_VERSION,
    EvaluationContract,
    attribute_failure,
)
from src.eval.models import CaseResult, EvalReport
from src.eval.patch_utils import apply_unified_patch
from src.repair.verification.termination import introduced_regression, regression_detected

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


def run_pytest(repo: Path, test_files: list[str] | None = None) -> tuple[int, str]:
    """在 repo 根目录运行 pytest，返回 (exit_code, combined_output)。"""
    targets = [str(item) for item in (test_files or []) if str(item).strip()]
    proc = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", *targets],
        cwd=repo,
        capture_output=True,
        text=True,
    )
    out = (proc.stdout or "") + (proc.stderr or "")
    return proc.returncode, out


def _environment_failure(output: str, returncode: int) -> bool:
    """Classify infrastructure/collection failures separately from test failures."""
    text = str(output or "").lower()
    if returncode in {3, 4, 5}:
        return True
    return any(
        marker in text
        for marker in (
            "internal error",
            "no module named 'pytest'",
            "could not load plugin",
            "docker",
            "network is unreachable",
            "permissionerror",
        )
    )


def _hash_text(value: str) -> str:
    return hashlib.sha256(str(value or "").encode("utf-8")).hexdigest()[:20]


def _trace_integrity(path: str) -> dict:
    if not path:
        return {"available": False, "valid": None, "issues": []}
    from agent_runtime.canonical_trace import validate_trace
    from agent_runtime.run_store import read_trace_path

    trace_path = Path(path)
    events: list[dict] = []
    for line in read_trace_path(trace_path):
        try:
            events.append(json.loads(line))
        except json.JSONDecodeError:
            pass
    issues = validate_trace(events, require_terminal=True)
    return {"available": True, "valid": not issues, "issues": issues}


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
    for key in ("parse_issue_ms", "baseline_ms"):
        if key in node_timings:
            result[key] = node_timings[key]
    for phase in PHASES:
        canon = phase_ms_key(phase)
        ms = get_phase_ms(node_timings, phase)
        if ms or canon in (node_timings.get("phases") or {}):
            result[canon] = ms
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

    def _start_evaluation_trace(self, eval_run_id: str, case_ids: list[str]) -> tuple[object, str]:
        """Create the evaluation-level trace and emit its start event."""
        from agent_runtime.canonical_trace import TraceSpanContext, reset_seq
        from agent_runtime.run_store import RunStore

        store = RunStore(str(self.output_dir))
        TraceSpanContext.reset()
        reset_seq(eval_run_id)
        store.start_run_by_id(eval_run_id)
        store.append_trace_event(
            eval_run_id,
            "evaluation_started",
            {
                "case_ids": list(case_ids),
                "case_count": len(case_ids),
                "contract_version": EVAL_CONTRACT_VERSION,
            },
            status="ok",
        )
        return store, str(store.runs_dir / eval_run_id / "trace.jsonl")

    @staticmethod
    def _emit_evaluation_event(
        store,
        eval_run_id: str,
        event: str,
        payload: dict,
        *,
        status: str | None = None,
    ) -> None:
        """Append an evaluation lifecycle event without affecting case traces."""
        store.append_trace_event(eval_run_id, event, payload, status=status)

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

    def _persist_run_artifacts(
        self,
        case_id: str,
        run_id: str,
        repo: Path,
        manifest: dict,
    ) -> tuple[str, str]:
        """Copy ephemeral run artifacts before the case workspace is removed."""
        if not run_id:
            return "", str(manifest.get("manifest_fingerprint", "") or "")
        source = repo / ".agent" / "runs" / run_id
        destination = self.output_dir / "runs" / case_id / run_id
        if source.is_dir():
            destination.parent.mkdir(parents=True, exist_ok=True)
            if destination.exists():
                shutil.rmtree(destination)
            shutil.copytree(source, destination)
        trace_path = destination / "trace.jsonl"
        if not trace_path.exists() and (destination / "trace.jsonl.gz").exists():
            trace_path = destination / "trace.jsonl.gz"
        manifest_path = destination / "manifest.json"
        if destination.is_dir():
            manifest_path.write_text(
                json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
        return str(trace_path) if trace_path.exists() else "", str(
            manifest.get("manifest_fingerprint", "") or ""
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

        eval_run_id = "eval-" + uuid.uuid4().hex[:16]
        trace_store, eval_trace_path = self._start_evaluation_trace(eval_run_id, ids)
        results: list[CaseResult] = []
        for case_id in ids:
            for run_idx in range(repetitions):
                if resume and (case_id, variant, run_idx) in completed:
                    continue
                result = self.run_case(case_id, eval_run_id=eval_run_id)
                result.run_index = run_idx
                results.append(result)
                self._emit_evaluation_event(
                    trace_store,
                    eval_run_id,
                    "evaluation_contract_checked",
                    {
                        "case_id": case_id,
                        "run_index": run_idx,
                        "passed": result.fixed,
                        "contract_required": result.contract_required,
                        "failure_class": result.failure_class,
                        "failure_code": result.failure_code,
                        "case_run_id": result.run_id,
                        "case_trace_path": result.trace_path,
                        "manifest_fingerprint": result.manifest_fingerprint,
                    },
                    status="ok" if result.fixed else "error",
                )
                if result.bad_case_id:
                    self._emit_evaluation_event(
                        trace_store,
                        eval_run_id,
                        "bad_case_created",
                        {
                            "case_id": case_id,
                            "bad_case_id": result.bad_case_id,
                            "failure_code": result.failure_code,
                            "case_run_id": result.run_id,
                        },
                        status="error",
                    )
                if resume:
                    self._save_checkpoint_entry(
                        case_id,
                        variant=variant,
                        rep=run_idx,
                    )

        if resume and not results:
            print("[eval] 所有 case 已完成", file=sys.stderr)
            report = build_eval_report([])
            report.eval_run_id = eval_run_id
            report.trace_path = eval_trace_path
            self._emit_evaluation_event(
                trace_store,
                eval_run_id,
                "evaluation_finished",
                {"status": "completed", "total": 0, "fixed": 0},
                status="ok",
            )
            return report

        report = build_eval_report(results)
        report.eval_run_id = eval_run_id
        report.trace_path = eval_trace_path
        self._emit_evaluation_event(
            trace_store,
            eval_run_id,
            "evaluation_finished",
            {
                "status": "completed",
                "total": len(results),
                "fixed": sum(1 for result in results if result.fixed),
                "failed": sum(1 for result in results if not result.fixed),
            },
            status="ok",
        )
        out = report_path or (self.output_dir / "eval_report.json")
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(report.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8")

        if resume:
            self._clear_checkpoint()
        return report

    def run_case(self, case_id: str, *, eval_run_id: str = "") -> CaseResult:
        """运行单个 Case：复制 repo → repair → pytest → 记录 diff 与指标。"""
        case_dir = self.cases_dir / case_id
        if not case_dir.is_dir():
            return CaseResult(
                case_id=case_id,
                error=f"unknown case: {case_id}",
                eval_run_id=eval_run_id,
            )

        meta = load_case_metadata(case_dir)
        language = str(meta.get("language", "python")).lower()
        is_fake = self._is_fake_mode()
        issue = build_case_issue(case_dir, metadata=meta)
        minimal_lines = _read_min_lines(case_dir)
        target_files = [str(item) for item in (meta.get("test_files") or [])]

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
            target_code, target_out = (
                (post_code, post_out)
                if not target_files or (is_fake and language != "python")
                else run_pytest(tmp_repo, target_files)
            )

            original_snapshot = Path(tmp) / "original"
            _copy_case_repo(case_dir / "repo", original_snapshot)
            actual_patch = collect_repo_diff(original_snapshot, tmp_repo)
            actual_lines = count_changed_lines(actual_patch)

            introduced_regression_flag = regression_detected(pre_code, post_code)
            if state and introduced_regression(state):
                introduced_regression_flag = True

            baseline_failed = pre_code != 0
            target_passed = target_code == 0
            regression_passed = not introduced_regression_flag
            environment_ok = not (
                _environment_failure(pre_out, pre_code)
                or _environment_failure(post_out, post_code)
                or _environment_failure(target_out, target_code)
            )
            fixed = target_passed and regression_passed and environment_ok

            retry_count = state.retry_count if state else 0
            status = state.status if state else ""
            failure_tags = list(state.failure_tags) if state else []
            total_tokens = 0
            token_usage: dict = {}
            cost_usd = 0.0
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
                cost_usd = float(
                    (getattr(state, "harness_metrics", {}) or {}).get("cost_usd", 0.0) or 0.0
                )
            if not error and state and getattr(state, "agent_errors", None):
                errs = state.agent_errors
                if errs:
                    error = "; ".join(f"{k}: {v}" for k, v in errs.items())

            status = str(getattr(state, "status", "") or "").strip()
            if not status:
                status = "fixed" if fixed else "failed"
            if status == "pending":
                status = "fixed" if fixed else "failed"

            run_id = str(getattr(state, "repair_run_id", "") or "")
            manifest = dict(getattr(state, "harness_manifest", {}) or {})
            if not manifest:
                manifest = {
                    "schema_version": 1,
                    "run_id": run_id,
                    "case_id": case_id,
                    "language": language,
                    "model": {"name": "", "provider": ""},
                    "prompt_hash": _hash_text(issue),
                    "runtime": {
                        "python": platform.python_version(),
                        "platform": platform.platform(),
                    },
                }
                manifest["manifest_fingerprint"] = _hash_text(
                    json.dumps(manifest, sort_keys=True, default=str)
                )
            trace_path, manifest_fingerprint = self._persist_run_artifacts(
                case_id,
                run_id,
                tmp_repo,
                manifest,
            )
            replay_meta = _trace_integrity(trace_path)
            if replay_meta.get("available") and not replay_meta.get("valid"):
                failure_tags.append("trace_invalid")

            contract = EvaluationContract(
                baseline_failed=baseline_failed,
                target_passed=target_passed,
                regression_passed=regression_passed,
                environment_ok=environment_ok,
                patch_present=bool(actual_patch.strip()),
            )
            contract_required = str(meta.get("status", "") or "").lower() == "verified"
            agent_error = bool(error)
            failure_class, failure_code = attribute_failure(
                contract,
                agent_error=agent_error,
            )
            if contract_required:
                fixed = contract.passed
            else:
                fixed = target_passed and regression_passed and environment_ok and not agent_error
                if fixed:
                    failure_class, failure_code = attribute_failure(
                        EvaluationContract(
                            baseline_failed=True,
                            target_passed=True,
                            regression_passed=True,
                            environment_ok=True,
                            patch_present=True,
                        )
                    )
            if not fixed:
                if contract_required and failure_class.value == "contract":
                    status = "contract_failed"
                if failure_code.value not in failure_tags:
                    failure_tags.append(failure_code.value)

            bad_case_id = ""
            if not fixed:
                from agent_runtime.harness_engineering import BadCaseRecord, BadCaseStore

                bad_case_id = "badcase-eval-" + _hash_text(
                    f"{case_id}:{run_id}:{failure_code.value}:{manifest_fingerprint}"
                )
                bad_case = BadCaseRecord(
                    badcase_id=bad_case_id,
                    run_id=run_id,
                    manifest_fingerprint=manifest_fingerprint,
                    primary_cause=failure_code.value,
                    contributing_causes=list(failure_tags),
                    evidence_refs=[f"case:{case_id}", f"contract:{EVAL_CONTRACT_VERSION}"],
                    trace_refs=[trace_path] if trace_path else [],
                )
                try:
                    BadCaseStore(self.output_dir).append(bad_case)
                except OSError:
                    pass

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
                cost_usd=cost_usd,
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
                language=language,
                tool_permission_profile=str(meta.get("tool_permission_profile", "") or ""),
                run_id=run_id,
                trace_path=trace_path,
                manifest_fingerprint=manifest_fingerprint,
                eval_contract_version=EVAL_CONTRACT_VERSION,
                contract_required=contract_required,
                baseline_failed=baseline_failed,
                target_passed=target_passed,
                regression_passed=regression_passed,
                environment_ok=environment_ok,
                failure_class=failure_class.value,
                failure_code=failure_code.value,
                bad_case_id=bad_case_id,
                replay=replay_meta,
                judge_metadata=(
                    dict(getattr(self.judge_client, "metadata", {}) or {})
                    if self.judge_client
                    else {}
                ),
                eval_run_id=eval_run_id,
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
