"""AblationRunner：多变体 × 多 Case × 多次重复的消融实验。"""

from __future__ import annotations

import json
import sys
from collections import defaultdict
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

from src.eval.models import CaseResult
from src.eval.runner import DEFAULT_CASES_DIR, EvalRunner


def build_ablation_report(results: list[CaseResult]) -> dict:
    """按变体聚合 fix_rate、avg_retries、avg_duration。"""
    by_variant: dict[str, list[CaseResult]] = defaultdict(list)
    for result in results:
        by_variant[result.variant].append(result)

    summary_by_variant: dict[str, dict] = {}
    for variant, variant_results in sorted(by_variant.items()):
        total = len(variant_results)
        fixed = sum(1 for r in variant_results if r.fixed)
        summary_by_variant[variant] = {
            "total": total,
            "fixed": fixed,
            "fix_rate": round(fixed / total, 4) if total else 0.0,
            "avg_retries": round(sum(r.retry_count for r in variant_results) / total, 2)
            if total
            else 0.0,
            "avg_duration_ms": round(
                sum(r.duration_ms for r in variant_results) / total,
                2,
            )
            if total
            else 0.0,
            "total_tokens": sum(r.total_tokens for r in variant_results),
            "avg_total_tokens": round(
                sum(r.total_tokens for r in variant_results) / total,
                2,
            )
            if total
            else 0.0,
        }

    return {
        "summary_by_variant": summary_by_variant,
        "runs": [r.to_dict() for r in results],
    }


def save_ablation_report(
    report_path: Path,
    results: list[CaseResult],
    *,
    meta: dict | None = None,
) -> dict:
    """写入 ablation_report.json（每次 run 完成后调用，支持断点恢复）。"""
    report = build_ablation_report(results)
    if meta:
        report["meta"] = meta
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    return report


def append_run_journal(journal_path: Path, result: CaseResult) -> None:
    """追加单条 run 到 JSONL 日志（实时持久化）。"""
    journal_path.parent.mkdir(parents=True, exist_ok=True)
    with journal_path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(result.to_dict(), ensure_ascii=False) + "\n")
        f.flush()


def _print_run_progress(
    completed: int,
    total_runs: int,
    variant: str,
    run_index: int,
    case_id: str,
    *,
    phase: str,
) -> None:
    prefix = f"[{completed}/{total_runs}]" if phase == "done" else f"[{completed + 1}/{total_runs}]"
    if phase == "start":
        print(
            f"{prefix} {variant} rep={run_index} {case_id} 开始...",
            file=sys.stderr,
            flush=True,
        )
    else:
        print(f"{prefix} {variant} rep={run_index} {case_id} 完成", file=sys.stderr, flush=True)


def _print_run_result(result: CaseResult) -> None:
    mark = "OK" if result.fixed else "FAIL"
    print(
        f"       -> [{mark}] fixed={result.fixed} status={result.status or '-'} "
        f"retries={result.retry_count} ms={result.duration_ms} tokens={result.total_tokens}",
        file=sys.stderr,
        flush=True,
    )
    if result.error:
        print(f"       error: {result.error[:200]}", file=sys.stderr, flush=True)


class AblationRunner:
    """消融实验运行器。"""

    def __init__(
        self,
        variants: dict[str, Callable[[str], object]],
        cases_dir: str | Path | None = None,
        output_dir: str | Path | None = None,
        skip_verify: bool = False,
    ):
        self.variants = variants
        self.cases_dir = Path(cases_dir or DEFAULT_CASES_DIR)
        self.output_dir = Path(output_dir or Path.cwd() / "eval_results")
        self.skip_verify = skip_verify

    def list_cases(self) -> list[str]:
        probe = EvalRunner(
            orchestrator_factory=next(iter(self.variants.values())),
            cases_dir=self.cases_dir,
        )
        return probe.list_cases()

    def run(
        self,
        case_ids: list[str] | None = None,
        repetitions: int = 3,
        report_path: Path | None = None,
        *,
        progress: bool = True,
    ) -> dict:
        ids = case_ids or self.list_cases()
        all_results: list[CaseResult] = []
        out = report_path or (self.output_dir / "ablation_report.json")
        journal_path = out.parent / "ablation_runs.jsonl"

        started_at = datetime.now(UTC).isoformat()
        total_runs = len(self.variants) * repetitions * len(ids)
        completed = 0

        if progress:
            print(
                f"Ablation 开始: {len(self.variants)} 变体 × {len(ids)} Case × {repetitions} 次 = {total_runs} 次",
                file=sys.stderr,
                flush=True,
            )
            print(f"实时报告: {out.resolve()}", file=sys.stderr, flush=True)
            print(f"运行日志: {journal_path.resolve()}", file=sys.stderr, flush=True)

        save_ablation_report(
            out,
            all_results,
            meta={
                "started_at": started_at,
                "updated_at": started_at,
                "completed_runs": 0,
                "total_runs": total_runs,
                "status": "running",
                "variants": list(self.variants.keys()),
                "case_ids": ids,
                "repetitions": repetitions,
            },
        )

        for variant_name, factory in self.variants.items():
            runner = EvalRunner(
                orchestrator_factory=factory,
                cases_dir=self.cases_dir,
                output_dir=self.output_dir / variant_name,
                skip_verify=self.skip_verify,
            )
            for run_index in range(repetitions):
                for case_id in ids:
                    if progress:
                        _print_run_progress(
                            completed,
                            total_runs,
                            variant_name,
                            run_index,
                            case_id,
                            phase="start",
                        )

                    result = runner.run_case(case_id)
                    result.variant = variant_name
                    result.run_index = run_index
                    all_results.append(result)
                    completed += 1

                    append_run_journal(journal_path, result)
                    report = save_ablation_report(
                        out,
                        all_results,
                        meta={
                            "started_at": started_at,
                            "updated_at": datetime.now(UTC).isoformat(),
                            "completed_runs": completed,
                            "total_runs": total_runs,
                            "status": "running" if completed < total_runs else "completed",
                            "variants": list(self.variants.keys()),
                            "case_ids": ids,
                            "repetitions": repetitions,
                        },
                    )

                    if progress:
                        _print_run_progress(
                            completed,
                            total_runs,
                            variant_name,
                            run_index,
                            case_id,
                            phase="done",
                        )
                        _print_run_result(result)
                        summary = report["summary_by_variant"].get(variant_name, {})
                        print(
                            f"       变体累计 fix_rate={summary.get('fix_rate', 0)} "
                            f"({summary.get('fixed', 0)}/{summary.get('total', 0)})",
                            file=sys.stderr,
                            flush=True,
                        )

        if progress:
            print("Ablation 完成。", file=sys.stderr, flush=True)

        return report
