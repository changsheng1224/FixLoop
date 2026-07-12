"""评测指标计算与 Markdown 报告生成。"""

from __future__ import annotations

import json
from collections import defaultdict
from pathlib import Path

from src.eval.models import CaseResult, EvalReport


def _summary_metrics(results: list[CaseResult]) -> dict:
    total = len(results)
    if total == 0:
        return {
            "total": 0,
            "fixed": 0,
            "fix_rate": 0.0,
            "first_attempt_rate": 0.0,
            "avg_retries": 0.0,
            "patch_precision": 0.0,
            "avg_duration_ms": 0,
            "avg_duration_s": 0.0,
            "regression_count": 0,
            "regression_rate": 0.0,
        }

    fixed_n = sum(1 for r in results if r.fixed)
    durations = [r.duration_ms for r in results]
    token_totals = [r.total_tokens for r in results if r.total_tokens > 0]

    summary = {
        "total": total,
        "fixed": fixed_n,
        "fix_rate": round(fixed_n / total, 4),
        "first_attempt_rate": round(
            sum(1 for r in results if r.fixed and r.retry_count == 0) / total,
            4,
        ),
        "avg_retries": round(sum(r.retry_count for r in results) / total, 2),
        "patch_precision": round(
            sum(r.minimal_lines / max(r.actual_lines, 1) for r in results) / total,
            4,
        ),
        "avg_duration_ms": int(sum(durations) / total),
        "avg_duration_s": round(sum(durations) / total / 1000, 2),
        "regression_count": sum(1 for r in results if r.introduced_regression),
        "regression_rate": round(
            sum(1 for r in results if r.introduced_regression) / total,
            4,
        ),
    }
    if token_totals:
        summary["total_tokens"] = sum(token_totals)
        summary["avg_total_tokens"] = round(sum(token_totals) / len(token_totals), 2)
    return summary


def _bucket_metrics(results: list[CaseResult], key_fn) -> dict[str, dict]:
    buckets: dict[str, list[CaseResult]] = defaultdict(list)
    for result in results:
        buckets[key_fn(result) or "unknown"].append(result)
    return {name: _summary_metrics(items) for name, items in sorted(buckets.items())}


def compute_metrics(results: list[CaseResult]) -> EvalReport:
    """从 CaseResult 列表计算完整 EvalReport。"""
    by_variant: dict[str, dict] = {}
    variants = sorted({r.variant for r in results if r.variant})
    for variant in variants:
        subset = [r for r in results if r.variant == variant]
        by_variant[variant] = _summary_metrics(subset)

    from src.eval.skill_metrics import skill_metrics_from_case_results

    skill_metrics = skill_metrics_from_case_results(results)
    report = EvalReport(
        cases=results,
        summary=_summary_metrics(results),
        by_type=_bucket_metrics(results, lambda r: r.issue_type),
        by_difficulty=_bucket_metrics(results, lambda r: r.difficulty),
        skill_metrics=skill_metrics,
    )
    if by_variant:
        report.by_variant = by_variant
    return report


def _markdown_table(headers: list[str], rows: list[list[str]]) -> str:
    lines = [
        "| " + " | ".join(headers) + " |",
        "| " + " | ".join("---" for _ in headers) + " |",
    ]
    for row in rows:
        lines.append("| " + " | ".join(row) + " |")
    return "\n".join(lines)


def format_markdown(report: EvalReport) -> str:
    """生成可粘贴到 README 的 Markdown 指标报告。"""
    parts: list[str] = ["# Eval Metrics Report", ""]

    summary = report.summary
    parts.append("## Overall")
    parts.append(
        _markdown_table(
            [
                "total",
                "fixed",
                "fix_rate",
                "first_attempt_rate",
                "avg_retries",
                "patch_precision",
                "avg_duration_s",
                "regression_rate",
            ],
            [
                [
                    str(summary.get("total", 0)),
                    str(summary.get("fixed", 0)),
                    f"{summary.get('fix_rate', 0):.2%}",
                    f"{summary.get('first_attempt_rate', 0):.2%}",
                    str(summary.get("avg_retries", 0)),
                    f"{summary.get('patch_precision', 0):.4f}",
                    str(summary.get("avg_duration_s", 0)),
                    f"{summary.get('regression_rate', 0):.2%}",
                ]
            ],
        )
    )
    parts.append("")

    by_variant = getattr(report, "by_variant", None) or {}
    if by_variant:
        parts.append("## By Variant")
        rows = []
        for variant, metrics in sorted(by_variant.items()):
            rows.append(
                [
                    variant,
                    str(metrics.get("fixed", 0)),
                    str(metrics.get("total", 0)),
                    f"{metrics.get('fix_rate', 0):.2%}",
                    str(metrics.get("avg_retries", 0)),
                    str(metrics.get("avg_duration_s", 0)),
                    f"{metrics.get('patch_precision', 0):.4f}",
                ]
            )
        parts.append(
            _markdown_table(
                [
                    "variant",
                    "fixed",
                    "total",
                    "fix_rate",
                    "avg_retries",
                    "avg_duration_s",
                    "patch_precision",
                ],
                rows,
            )
        )
        parts.append("")

    if report.cases:
        parts.append("## By Case")
        rows = []
        for case in report.cases:
            rows.append(
                [
                    case.case_id,
                    case.variant or "-",
                    "yes" if case.fixed else "no",
                    str(case.retry_count),
                    str(case.actual_lines),
                    str(case.minimal_lines),
                    str(case.duration_ms),
                    str(case.total_tokens or "-"),
                ]
            )
        parts.append(
            _markdown_table(
                [
                    "case_id",
                    "variant",
                    "fixed",
                    "retries",
                    "actual_lines",
                    "minimal_lines",
                    "duration_ms",
                    "tokens",
                ],
                rows,
            )
        )
        parts.append("")

    if report.by_type:
        parts.append("## By Issue Type")
        rows = [
            [
                issue_type,
                str(metrics.get("fixed", 0)),
                str(metrics.get("total", 0)),
                f"{metrics.get('fix_rate', 0):.2%}",
                f"{metrics.get('patch_precision', 0):.2%}",
            ]
            for issue_type, metrics in sorted(report.by_type.items())
        ]
        parts.append(_markdown_table(["issue_type", "fixed", "total", "fix_rate", "precision"], rows))
        parts.append("")

    if report.by_difficulty:
        parts.append("## By Difficulty")
        rows = [
            [
                difficulty,
                str(metrics.get("fixed", 0)),
                str(metrics.get("total", 0)),
                f"{metrics.get('fix_rate', 0):.2%}",
                f"{metrics.get('patch_precision', 0):.2%}",
            ]
            for difficulty, metrics in sorted(report.by_difficulty.items())
        ]
        parts.append(_markdown_table(["difficulty", "fixed", "total", "fix_rate", "precision"], rows))

    return "\n".join(parts).strip() + "\n"


def case_result_from_dict(data: dict) -> CaseResult:
    """从 JSON dict 还原 CaseResult（忽略 actual_patch 大字段）。"""
    return CaseResult(
        case_id=str(data.get("case_id", "")),
        issue_type=str(data.get("issue_type", "")),
        difficulty=str(data.get("difficulty", "")),
        fixed=bool(data.get("fixed", False)),
        retry_count=int(data.get("retry_count", 0)),
        actual_lines=int(data.get("actual_lines", 0)),
        minimal_lines=int(data.get("minimal_lines", 0)),
        duration_ms=int(data.get("duration_ms", 0)),
        agent_timings=dict(data.get("agent_timings") or {}),
        error=str(data.get("error", "")),
        introduced_regression=bool(data.get("introduced_regression", False)),
        status=str(data.get("status", "")),
        variant=str(data.get("variant", "")),
        run_index=int(data.get("run_index", 0)),
        total_tokens=int(data.get("total_tokens", 0) or 0),
        token_usage=dict(data.get("token_usage") or {}),
        expected_skill=data.get("expected_skill"),
        matched_skill=data.get("matched_skill"),
        skill_match=bool(data.get("skill_match", False)),
    )


def load_results_from_json_report(path: str | Path) -> tuple[list[CaseResult], dict]:
    """从 eval_report.json 或 ablation_report.json 加载 runs/cases。"""
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    if "runs" in data:
        results = [case_result_from_dict(item) for item in data["runs"]]
        meta = data.get("meta") or {}
        return results, meta
    results = [case_result_from_dict(item) for item in data.get("cases", [])]
    return results, {}


def format_run_notes(
    meta: dict | None,
    summary: dict,
    results: list[CaseResult] | None = None,
    *,
    extra_lines: list[str] | None = None,
) -> str:
    """生成首轮/单次实验的文字分析摘要。"""
    meta = meta or {}
    total = summary.get("total", 0)
    fixed = summary.get("fixed", 0)
    fix_rate = summary.get("fix_rate", 0.0)
    lines = [
        "## Run Notes",
        "",
        f"- **配置**: {meta.get('variants', ['eval'])} × {meta.get('case_ids', ['all'])} "
        f"× {meta.get('repetitions', 1)} 次",
        f"- **Fix Rate**: {fixed}/{total} ({fix_rate:.1%})",
        f"- **平均耗时**: {summary.get('avg_duration_s', 0)}s",
        f"- **平均重试**: {summary.get('avg_retries', 0)}",
    ]
    if summary.get("total_tokens"):
        lines.append(f"- **总 Token**: {summary['total_tokens']}")
    elif results and all(r.total_tokens == 0 for r in results):
        lines.append("- **Token**: 未记录（JSON 生成于 token 追踪启用前）")

    if results:
        by_variant: dict[str, list[CaseResult]] = defaultdict(list)
        for result in results:
            if result.variant:
                by_variant[result.variant].append(result)
        if len(by_variant) > 1:
            parts = []
            for variant in sorted(by_variant):
                subset = by_variant[variant]
                avg_s = sum(r.duration_ms for r in subset) / len(subset) / 1000
                parts.append(f"{variant} ~{avg_s:.1f}s")
            lines.append(f"- **变体耗时**: {', '.join(parts)}")

        inflated = any(r.actual_lines > max(r.minimal_lines * 10, 10) for r in results)
        if inflated:
            lines.append(
                "- **patch_precision 说明**: 本 JSON 的 actual_lines 含 `.agent` 噪声，"
                "指标偏低；重跑评测或启用 diff 过滤后更准确"
            )

    if meta.get("started_at"):
        lines.append(f"- **开始时间**: {meta['started_at']}")
    if meta.get("updated_at"):
        lines.append(f"- **结束时间**: {meta['updated_at']}")

    if extra_lines:
        lines.extend(extra_lines)

    lines.append("")
    return "\n".join(lines)


def write_metrics_markdown(
    results: list[CaseResult],
    markdown_path: str | Path,
    *,
    meta: dict | None = None,
    notes: str = "",
    extra_lines: list[str] | None = None,
) -> Path:
    """计算指标并写入 Markdown 报告文件。"""
    report = compute_metrics(results)
    parts: list[str] = []
    if notes:
        parts.append(notes.rstrip())
        parts.append("")
    elif meta:
        parts.append(
            format_run_notes(meta, report.summary, results, extra_lines=extra_lines).rstrip()
        )
        parts.append("")
    parts.append(format_markdown(report).rstrip())

    out = Path(markdown_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text("\n".join(parts) + "\n", encoding="utf-8")
    return out


def write_metrics_markdown_from_report(
    json_report_path: str | Path,
    markdown_path: str | Path,
    *,
    notes: str = "",
    extra_lines: list[str] | None = None,
) -> Path:
    """从已有 eval/ablation JSON 报告生成 Markdown。"""
    results, meta = load_results_from_json_report(json_report_path)
    return write_metrics_markdown(
        results, markdown_path, meta=meta, notes=notes, extra_lines=extra_lines
    )
