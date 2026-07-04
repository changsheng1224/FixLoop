"""eval 子命令共享逻辑（src.cli 与 src.eval.runner 复用）。"""

from __future__ import annotations

import json
import sys
from pathlib import Path

from src.eval.fake_runner import fake_orchestrator_factory
from src.eval.models import EvalReport
from src.eval.runner import DEFAULT_CASES_DIR, EvalRunner
from src.repair_factory import make_orchestrator_factory


def resolve_json_output_path(output: str, default_filename: str) -> tuple[Path, Path]:
    """解析 --output：可为目录或 .json 文件路径。"""
    out = Path(output)
    if output.endswith(".json"):
        return out.parent if str(out.parent) not in ("", ".") else Path("eval_results"), out
    return out, out / default_filename


def resolve_report_path(output: str) -> tuple[Path, Path]:
    """解析 eval --output，返回 (output_dir, eval_report.json 路径)。"""
    return resolve_json_output_path(output, "eval_report.json")


def print_eval_report(report: EvalReport, verbose: bool, report_path: Path) -> None:
    """打印 summary JSON；verbose 时逐 Case 详情写 stderr。"""
    if verbose:
        for c in report.cases:
            mark = "OK" if c.fixed else "FAIL"
            print(
                f"[{mark}] {c.case_id} type={c.issue_type} "
                f"fixed={c.fixed} retries={c.retry_count} "
                f"lines={c.actual_lines}/{c.minimal_lines} ms={c.duration_ms} "
                f"tokens={c.total_tokens}",
                file=sys.stderr,
            )
            if c.agent_timings:
                print(f"       timings: {c.agent_timings}", file=sys.stderr)
            if c.error:
                print(f"       error: {c.error}", file=sys.stderr)

    print(json.dumps(report.summary, ensure_ascii=False))
    print(f"Report: {report_path.resolve()}", file=sys.stderr)


def resolve_markdown_path(output_dir: Path, markdown: str | None) -> Path | None:
    """将 --markdown 参数解析为 .md 输出路径。"""
    if markdown is None:
        return None
    candidate = Path(markdown)
    if candidate.suffix == ".md" and str(candidate.parent) not in ("", "."):
        return candidate
    return output_dir / markdown


def run_eval(
    *,
    case_ids: list[str] | None,
    cases_dir: str | Path = DEFAULT_CASES_DIR,
    output: str = "eval_results",
    verbose: bool = False,
    fake: bool = False,
    skip_verify: bool = False,
    model_client=None,
    markdown: str | None = None,
) -> tuple[EvalReport, Path, int]:
    """执行 eval 子命令：跑 Case、写报告，返回 (report, path, exit_code)。"""
    output_dir, report_path = resolve_report_path(output)

    if fake:
        factory = fake_orchestrator_factory(cases_dir)
    else:
        factory = make_orchestrator_factory(skip_verify=skip_verify, model_client=model_client)

    runner = EvalRunner(
        orchestrator_factory=factory,
        cases_dir=cases_dir,
        output_dir=output_dir,
        skip_verify=skip_verify,
    )
    ids = runner.list_cases() if case_ids is None else case_ids
    report = runner.run_all(ids, report_path=report_path)

    if markdown is not None:
        from src.eval.metrics import write_metrics_markdown

        md_path = resolve_markdown_path(output_dir, markdown)
        write_metrics_markdown(report.cases, md_path)
        print(f"Markdown: {md_path.resolve()}", file=sys.stderr)

    exit_code = 0 if report.summary.get("fixed") == report.summary.get("total") else 1
    return report, report_path, exit_code


def resolve_ablation_report_path(output: str) -> tuple[Path, Path]:
    """解析 ablation --output，返回 (output_dir, ablation_report.json 路径)。"""
    return resolve_json_output_path(output, "ablation_report.json")


def print_ablation_report(report: dict, verbose: bool, report_path: Path) -> None:
    """打印 summary_by_variant JSON；verbose 时逐变体写 stderr。"""
    if verbose:
        for variant, summary in report.get("summary_by_variant", {}).items():
            print(
                f"[{variant}] fix_rate={summary['fix_rate']} "
                f"fixed={summary['fixed']}/{summary['total']} "
                f"avg_retries={summary['avg_retries']} "
                f"avg_ms={summary['avg_duration_ms']} "
                f"avg_tokens={summary.get('avg_total_tokens', 0)}",
                file=sys.stderr,
            )

    print(json.dumps(report.get("summary_by_variant", {}), ensure_ascii=False))
    print(f"Report: {report_path.resolve()}", file=sys.stderr)


def run_ablation(
    *,
    case_ids: list[str] | None,
    cases_dir: str | Path = DEFAULT_CASES_DIR,
    output: str = "eval_results",
    verbose: bool = False,
    fake: bool = False,
    skip_verify: bool = False,
    repetitions: int = 3,
    variant_names: list[str] | None = None,
    progress: bool = True,
    model_client=None,
    markdown: str | None = None,
) -> tuple[dict, Path, int]:
    """执行 ablation 子命令，返回 (report, path, exit_code)。"""
    from src.eval.ablation import AblationRunner
    from src.eval.variants import build_ablation_variants

    output_dir, report_path = resolve_ablation_report_path(output)
    variants = build_ablation_variants(
        fake=fake,
        skip_verify=skip_verify,
        model_client=model_client,
        cases_dir=cases_dir,
        variant_names=variant_names,
    )
    runner = AblationRunner(
        variants=variants,
        cases_dir=cases_dir,
        output_dir=output_dir,
        skip_verify=skip_verify,
    )
    ids = runner.list_cases() if case_ids is None else case_ids
    report = runner.run(
        ids,
        repetitions=repetitions,
        report_path=report_path,
        progress=progress,
    )

    if markdown is not None:
        from src.eval.metrics import write_metrics_markdown_from_report

        md_path = resolve_markdown_path(output_dir, markdown)
        write_metrics_markdown_from_report(report_path, md_path)
        print(f"Markdown: {md_path.resolve()}", file=sys.stderr)

    total = sum(s["total"] for s in report["summary_by_variant"].values())
    fixed = sum(s["fixed"] for s in report["summary_by_variant"].values())
    exit_code = 0 if fixed == total else 1
    return report, report_path, exit_code
