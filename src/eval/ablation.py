"""AblationRunner：多变体 × 多 Case × 多次重复的消融实验。"""

from __future__ import annotations

import json
from collections import defaultdict
from collections.abc import Callable
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
        }

    return {
        "summary_by_variant": summary_by_variant,
        "runs": [r.to_dict() for r in results],
    }


class AblationRunner:
    """消融实验运行器。"""

    def __init__(
        self,
        variants: dict[str, Callable[[str], object]],
        cases_dir: str | Path | None = None,
        output_dir: str | Path | None = None,
        skip_verify: bool = True,
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
    ) -> dict:
        ids = case_ids or self.list_cases()
        all_results: list[CaseResult] = []

        for variant_name, factory in self.variants.items():
            runner = EvalRunner(
                orchestrator_factory=factory,
                cases_dir=self.cases_dir,
                output_dir=self.output_dir / variant_name,
                skip_verify=self.skip_verify,
            )
            for run_index in range(repetitions):
                for case_id in ids:
                    result = runner.run_case(case_id)
                    result.variant = variant_name
                    result.run_index = run_index
                    all_results.append(result)

        report = build_ablation_report(all_results)
        out = report_path or (self.output_dir / "ablation_report.json")
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        return report
