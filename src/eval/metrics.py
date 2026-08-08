"""评测指标计算与 Markdown 报告生成。"""

from __future__ import annotations

import json
import math
import re
from collections import Counter, defaultdict
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
            "duration_ms_p50": 0,
            "duration_ms_p95": 0,
            "token_p50": 0,
            "token_p95": 0,
            "regression_count": 0,
            "regression_rate": 0.0,
            "status_counts": {},
            "failure_tag_counts": {},
            "failure_reason_counts": {},
            "fix_rate_ci95": [0.0, 0.0],
            "regression_rate_ci95": [0.0, 0.0],
            "duration_mean_ci95": [0.0, 0.0],
            "cost_mean_ci95": [0.0, 0.0],
        }

    fixed_n = sum(1 for r in results if r.fixed)
    durations = [r.duration_ms for r in results]
    token_totals = [r.total_tokens for r in results if r.total_tokens > 0]
    costs = [r.cost_usd for r in results if r.cost_usd >= 0]
    status_counts = Counter(_case_status(r) for r in results)
    failure_tag_counts = Counter(tag for r in results for tag in r.failure_tags)
    failure_reason_counts = Counter(
        _failure_reason(r) for r in results if not r.fixed and _failure_reason(r)
    )

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
        "duration_ms_p50": _p50(durations),
        "duration_ms_p95": _percentile(durations, 95),
        "regression_count": sum(1 for r in results if r.introduced_regression),
        "regression_rate": round(
            sum(1 for r in results if r.introduced_regression) / total,
            4,
        ),
        "status_counts": dict(sorted(status_counts.items())),
        "failure_tag_counts": dict(sorted(failure_tag_counts.items())),
        "failure_reason_counts": dict(sorted(failure_reason_counts.items())),
        "fix_rate_ci95": list(_wilson_interval(fixed_n, total)),
        "regression_rate_ci95": list(
            _wilson_interval(sum(1 for r in results if r.introduced_regression), total)
        ),
        "duration_mean_ci95": list(_mean_interval(durations)),
        "cost_mean_usd": round(sum(costs) / len(costs), 6) if costs else 0.0,
        "cost_mean_ci95": list(_mean_interval(costs)),
    }
    if token_totals:
        summary["total_tokens"] = sum(token_totals)
        summary["avg_total_tokens"] = round(sum(token_totals) / len(token_totals), 2)
        summary["token_p50"] = _p50(token_totals)
        summary["token_p95"] = _percentile(token_totals, 95)
    else:
        summary["token_p50"] = 0
        summary["token_p95"] = 0

    # patch equivalence
    eq_full = sum(1 for r in results if r.equivalence == "full")
    eq_partial = sum(1 for r in results if r.equivalence == "partial")
    summary["equivalence_by_type"] = {
        "full": eq_full,
        "partial": eq_partial,
        "none": total - eq_full - eq_partial,
    }
    summary["avg_equivalence_full_rate"] = round(eq_full / total, 4)
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
    pk = compute_pass_at_k(results)
    perf = compute_performance_matrix(results)
    judge = compute_judge_summary(results)
    report = EvalReport(
        cases=results,
        summary=_summary_metrics(results),
        by_type=_bucket_metrics(results, lambda r: r.issue_type),
        by_difficulty=_bucket_metrics(results, lambda r: r.difficulty),
        skill_metrics=skill_metrics,
        pass_at_k=pk,
        performance=perf,
        judge_summary=judge,
        by_language=_bucket_metrics(results, lambda r: r.language),
        by_failure_class=_bucket_metrics(results, lambda r: r.failure_class),
        by_permission_profile=_bucket_metrics(
            results, lambda r: r.tool_permission_profile
        ),
    )
    if by_variant:
        report.by_variant = by_variant
    return report


def _case_status(result: CaseResult) -> str:
    if result.status:
        return result.status
    return "fixed" if result.fixed else "failed"


def _patch_precision(result: CaseResult) -> float:
    return round(result.minimal_lines / max(result.actual_lines, 1), 4)


def _failure_reason(result: CaseResult) -> str:
    text = " ".join(
        [
            result.error or "",
            result.status or "",
            " ".join(result.failure_tags or []),
        ]
    ).lower()
    if not text.strip():
        return "unknown"
    if "no patches" in text or "no patch" in text or "parse_fail" in text:
        return "no_patch"
    if "timeout" in text:
        return "timeout"
    if "sandbox" in text or "docker" in text:
        return "sandbox"
    if "regression" in text:
        return "regression"
    if "permission" in text or "denied" in text:
        return "permission"
    if "exhausted" in text:
        return "exhausted"
    return re.sub(r"[^a-z0-9_]+", "_", text.split(":", 1)[0]).strip("_") or "unknown"


def compute_performance_matrix(results: list[CaseResult]) -> dict:
    """计算性能矩阵：context_tokens, cache_hit_rate, p50_ttft_ms, tool_steps, repair_retries。

    数据来源：CaseResult.agent_timings（由 extract_agent_timings 填充）。
    """
    if not results:
        return {
            "avg_context_tokens": 0,
            "avg_cache_hit_rate": 0.0,
            "p50_ttft_ms": 0,
            "avg_tool_steps": 0.0,
            "avg_repair_retries": 0.0,
        }

    total = len(results)
    context_tokens = [r.agent_timings.get("context_tokens", 0) or 0 for r in results]
    cache_hits = [r.agent_timings.get("cache_hit_rate", 0) or 0 for r in results]
    tool_steps = [r.agent_timings.get("total_tool_steps", 0) or 0 for r in results]
    retries = [r.retry_count for r in results]

    # p50 ttft：从所有 case 的 ttft_values 中收集并取中位数
    all_ttft: list[int] = []
    for r in results:
        vals = r.agent_timings.get("ttft_values", []) or []
        all_ttft.extend(int(v) for v in vals if v)
    p50_ttft = _p50(all_ttft) if all_ttft else 0

    return {
        "avg_context_tokens": round(sum(context_tokens) / total, 2),
        "avg_cache_hit_rate": round(sum(cache_hits) / total, 4),
        "p50_ttft_ms": p50_ttft,
        "avg_tool_steps": round(sum(tool_steps) / total, 2),
        "avg_repair_retries": round(sum(retries) / total, 2),
    }


def _p50(values: list[int]) -> int:
    """计算中位数（p50）。"""
    if not values:
        return 0
    s = sorted(values)
    n = len(s)
    if n % 2 == 0:
        return (s[n // 2 - 1] + s[n // 2]) // 2
    return s[n // 2]


def _wilson_interval(successes: int, total: int, z: float = 1.96) -> tuple[float, float]:
    """Wilson score interval for small evaluation samples."""
    if total <= 0:
        return 0.0, 0.0
    p = successes / total
    denominator = 1 + (z * z / total)
    center = (p + z * z / (2 * total)) / denominator
    margin = (
        z
        * math.sqrt((p * (1 - p) / total) + (z * z / (4 * total * total)))
        / denominator
    )
    return round(max(0.0, center - margin), 4), round(min(1.0, center + margin), 4)


def _mean_interval(values: list[float], z: float = 1.96) -> tuple[float, float]:
    """Normal-approximation 95% interval for a sample mean."""
    if not values:
        return 0.0, 0.0
    mean = sum(values) / len(values)
    if len(values) == 1:
        return round(mean, 4), round(mean, 4)
    variance = sum((value - mean) ** 2 for value in values) / (len(values) - 1)
    margin = z * math.sqrt(variance / len(values))
    return round(max(0.0, mean - margin), 4), round(mean + margin, 4)


def _percentile(values: list[int], percentile: int) -> int:
    """Nearest-rank percentile for small eval samples."""
    if not values:
        return 0
    s = sorted(values)
    rank = max(1, math.ceil((percentile / 100) * len(s)))
    return s[min(rank - 1, len(s) - 1)]


def compute_judge_summary(results: list[CaseResult]) -> dict:
    """汇总 LLM-as-Judge 评分并与 patch_precision 对照。

    仅统计 judge_score > 0 的 case（未启用 judge 时为空）。
    """
    judged = [r for r in results if r.judge_score > 0]
    if not judged:
        return {}

    from src.eval.judge import JudgeClient

    avg_score = round(sum(r.judge_score for r in judged) / len(judged), 2)
    aligned = 0
    for r in judged:
        precision = r.minimal_lines / max(r.actual_lines, 1)
        comparison = JudgeClient.compare_with_precision(r.judge_score, precision)
        if comparison == "aligned":
            aligned += 1

    return {
        "judged_cases": len(judged),
        "avg_judge_score": avg_score,
        "aligned_with_precision": aligned,
        "alignment_rate": round(aligned / len(judged), 4) if judged else 0.0,
    }


def compute_pass_at_k(results: list[CaseResult]) -> dict[str, object]:
    """计算 Pass@k 指标。

    对每个 case_id，收集所有 run_index 的结果。若任一 run 通过（fixed=True），
    则该 case 在 k 下通过。返回 {"pass@1": ..., "pass@3": ...}。

    k 值取 min(最大 run_index+1, 目标 k)。
    若所有 case 都只有 1 次 run，则 pass@3 = pass@1。
    """
    if not results:
        return {"pass@1": 0.0, "pass@3": 0.0}

    # 按 case_id 分组
    by_case: dict[str, list[CaseResult]] = {}
    for r in results:
        by_case.setdefault(r.case_id, []).append(r)

    def _pass_at(k: int) -> tuple[float, int]:
        estimates: list[float] = []
        for runs in by_case.values():
            samples = runs[:k]
            n = len(samples)
            c = sum(1 for r in samples if r.fixed)
            if n == 0:
                continue
            if n < k:
                estimate = 1.0 if c else 0.0
            elif c == 0:
                estimate = 0.0
            else:
                # Standard unbiased Pass@k estimator.
                denominator = math.comb(n, k)
                estimate = 1.0 - (math.comb(n - c, k) / denominator if n - c >= k else 0.0)
            estimates.append(estimate)
        return (
            round(sum(estimates) / len(estimates), 4) if estimates else 0.0,
            len(estimates),
        )

    values: dict[str, float] = {}
    for k in (1, 3):
        estimate, sample_count = _pass_at(k)
        values[f"pass@{k}"] = estimate
        values[f"pass@{k}_sample_count"] = sample_count
        values[f"pass@{k}_ci95"] = list(
            _wilson_interval(round(estimate * sample_count), sample_count)
        )
    values["definition"] = "standard_unbiased_when_n_ge_k_else_empirical"
    return values


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
                "duration_p50_s",
                "duration_p95_s",
                "token_p50",
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
                    f"{summary.get('duration_ms_p50', 0) / 1000:.2f}",
                    f"{summary.get('duration_ms_p95', 0) / 1000:.2f}",
                    str(summary.get("token_p50", 0)),
                    f"{summary.get('regression_rate', 0):.2%}",
                ]
            ],
        )
    )
    parts.append("")

    parts.append("## Confidence & Cost")
    parts.append(
        _markdown_table(
            ["metric", "value"],
            [
                ["fix_rate_ci95", str(summary.get("fix_rate_ci95", [0.0, 0.0]))],
                [
                    "regression_rate_ci95",
                    str(summary.get("regression_rate_ci95", [0.0, 0.0])),
                ],
                ["duration_mean_ci95_ms", str(summary.get("duration_mean_ci95", [0.0, 0.0]))],
                ["cost_mean_usd", str(summary.get("cost_mean_usd", 0.0))],
                ["cost_mean_ci95_usd", str(summary.get("cost_mean_ci95", [0.0, 0.0]))],
            ],
        )
    )
    parts.append("")

    if report.pass_at_k:
        parts.append("## Pass@k")
        parts.append(
            _markdown_table(
                ["metric", "value"],
                [
                    [name, str(value)]
                    for name, value in sorted(report.pass_at_k.items())
                    if name != "definition"
                ]
                + [["definition", str(report.pass_at_k.get("definition", ""))]],
            )
        )
        parts.append("")

    parts.append("## Performance Detail")
    parts.append(
        _markdown_table(
            [
                "duration_ms_p50",
                "duration_ms_p95",
                "token_p50",
                "token_p95",
                "avg_total_tokens",
                "avg_tool_steps",
                "p50_ttft_ms",
            ],
            [
                [
                    str(summary.get("duration_ms_p50", 0)),
                    str(summary.get("duration_ms_p95", 0)),
                    str(summary.get("token_p50", 0)),
                    str(summary.get("token_p95", 0)),
                    str(summary.get("avg_total_tokens", 0)),
                    str((report.performance or {}).get("avg_tool_steps", 0)),
                    str((report.performance or {}).get("p50_ttft_ms", 0)),
                ]
            ],
        )
    )
    parts.append("")

    failure_rows: list[list[str]] = []
    for name, count in sorted((summary.get("status_counts") or {}).items()):
        failure_rows.append(["status", name, str(count)])
    for name, count in sorted((summary.get("failure_tag_counts") or {}).items()):
        failure_rows.append(["failure_tag", name, str(count)])
    for name, count in sorted((summary.get("failure_reason_counts") or {}).items()):
        failure_rows.append(["failure_reason", name, str(count)])
    if failure_rows:
        parts.append("## Failure Breakdown")
        parts.append(_markdown_table(["kind", "name", "count"], failure_rows))
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
                    f"{_patch_precision(case):.4f}",
                    str(case.duration_ms),
                    str(case.total_tokens or "-"),
                    _case_status(case),
                    ",".join(case.failure_tags) if case.failure_tags else "-",
                    (case.error or "-").replace("\n", " ")[:80],
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
                    "patch_precision",
                    "duration_ms",
                    "tokens",
                    "status",
                    "failure_tags",
                    "error",
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
        parts.append(
            _markdown_table(["issue_type", "fixed", "total", "fix_rate", "precision"], rows)
        )
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
        parts.append(
            _markdown_table(["difficulty", "fixed", "total", "fix_rate", "precision"], rows)
        )

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
        cost_usd=float(data.get("cost_usd", 0.0) or 0.0),
        agent_timings=dict(data.get("agent_timings") or {}),
        error=str(data.get("error", "")),
        introduced_regression=bool(data.get("introduced_regression", False)),
        status=str(data.get("status", "")),
        failure_tags=list(data.get("failure_tags") or []),
        variant=str(data.get("variant", "")),
        run_index=int(data.get("run_index", 0)),
        total_tokens=int(data.get("total_tokens", 0) or 0),
        token_usage=dict(data.get("token_usage") or {}),
        permission_denied_by_tool=dict(data.get("permission_denied_by_tool") or {}),
        expected_skill=data.get("expected_skill"),
        matched_skill=data.get("matched_skill"),
        skill_match=bool(data.get("skill_match", False)),
        judge_score=int(data.get("judge_score", 0) or 0),
        judge_reason=str(data.get("judge_reason", "")),
        equivalence=str(data.get("equivalence", "")),
        language=str(data.get("language", "")),
        tool_permission_profile=str(data.get("tool_permission_profile", "")),
        run_id=str(data.get("run_id", "")),
        trace_path=str(data.get("trace_path", "")),
        manifest_fingerprint=str(data.get("manifest_fingerprint", "")),
        eval_contract_version=str(data.get("eval_contract_version", "1.0")),
        contract_required=bool(data.get("contract_required", True)),
        baseline_failed=bool(data.get("baseline_failed", False)),
        target_passed=bool(data.get("target_passed", False)),
        regression_passed=bool(data.get("regression_passed", True)),
        environment_ok=bool(data.get("environment_ok", True)),
        failure_class=str(data.get("failure_class", "none")),
        failure_code=str(data.get("failure_code", "none")),
        bad_case_id=str(data.get("bad_case_id", "")),
        replay=dict(data.get("replay") or {}),
        judge_metadata=dict(data.get("judge_metadata") or {}),
        eval_run_id=str(data.get("eval_run_id", "")),
    )


def load_results_from_json_report(path: str | Path) -> tuple[list[CaseResult], dict]:
    """从 eval_report.json 或 ablation_report.json 加载 runs/cases。"""
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    if "runs" in data:
        results = [case_result_from_dict(item) for item in data["runs"]]
        meta = data.get("meta") or {}
        return results, meta
    results = [case_result_from_dict(item) for item in data.get("cases", [])]
    return results, {
        "eval_run_id": str(data.get("eval_run_id", "")),
        "trace_path": str(data.get("trace_path", "")),
    }


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
