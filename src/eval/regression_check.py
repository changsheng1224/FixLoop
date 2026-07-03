"""CI 回归门禁：对比当前评测与基线 fix_rate / regression_rate。"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass, field
from pathlib import Path

from src.eval.metrics import compute_metrics, load_results_from_json_report


@dataclass
class RegressionIssue:
    """单项指标回归。"""

    metric: str
    baseline: float
    current: float
    delta_pp: float
    threshold_pp: float
    message: str


@dataclass
class RegressionDetected:
    """检测到回归时的详情。"""

    issues: list[RegressionIssue] = field(default_factory=list)
    current_summary: dict = field(default_factory=dict)
    baseline_summary: dict = field(default_factory=dict)

    @property
    def detected(self) -> bool:
        return bool(self.issues)


@dataclass
class RegressionCheckResult:
    """回归检查结果（通过时 issues 为空）。"""

    passed: bool
    issues: list[RegressionIssue] = field(default_factory=list)
    current_summary: dict = field(default_factory=dict)
    baseline_summary: dict = field(default_factory=dict)

    def to_detected(self) -> RegressionDetected | None:
        if self.passed:
            return None
        return RegressionDetected(
            issues=list(self.issues),
            current_summary=dict(self.current_summary),
            baseline_summary=dict(self.baseline_summary),
        )


class RegressionChecker:
    """对比两次评测 summary，fix_rate 下降或 regression_rate 上升超阈值则判定回归。"""

    def __init__(
        self,
        *,
        fix_rate_drop_threshold_pp: float = 5.0,
        regression_rate_rise_threshold_pp: float = 3.0,
    ):
        self.fix_rate_drop_threshold_pp = fix_rate_drop_threshold_pp
        self.regression_rate_rise_threshold_pp = regression_rate_rise_threshold_pp

    def load_summary(self, report: str | Path | dict) -> dict:
        """从 JSON 路径或已解析 dict 提取 summary（含 regression_rate）。"""
        if isinstance(report, dict):
            if "summary" in report and report["summary"]:
                return dict(report["summary"])
            if "runs" in report or "cases" in report:
                results, _ = self._results_from_dict(report)
                return compute_metrics(results).summary
            raise ValueError("report dict 缺少 summary 或 runs/cases")

        path = Path(report)
        data = json.loads(path.read_text(encoding="utf-8"))
        if data.get("summary"):
            return dict(data["summary"])
        results, _ = load_results_from_json_report(path)
        if not results:
            raise ValueError(f"无法从 {path} 解析评测结果")
        return compute_metrics(results).summary

    @staticmethod
    def _results_from_dict(data: dict):
        from src.eval.metrics import case_result_from_dict

        rows = data.get("runs") or data.get("cases") or []
        return [case_result_from_dict(item) for item in rows], data.get("meta") or {}

    def check(
        self,
        current_report: str | Path | dict,
        baseline_report: str | Path | dict,
    ) -> RegressionCheckResult:
        """对比 current 与 baseline，超阈值返回 failed RegressionCheckResult。"""
        current = self.load_summary(current_report)
        baseline = self.load_summary(baseline_report)
        issues: list[RegressionIssue] = []

        fix_drop_pp = (baseline.get("fix_rate", 0.0) - current.get("fix_rate", 0.0)) * 100
        if fix_drop_pp > self.fix_rate_drop_threshold_pp:
            issues.append(
                RegressionIssue(
                    metric="fix_rate",
                    baseline=float(baseline.get("fix_rate", 0.0)),
                    current=float(current.get("fix_rate", 0.0)),
                    delta_pp=round(fix_drop_pp, 2),
                    threshold_pp=self.fix_rate_drop_threshold_pp,
                    message=(
                        f"Fix Rate regression: {baseline.get('fix_rate', 0):.1%} → "
                        f"{current.get('fix_rate', 0):.1%} "
                        f"(-{fix_drop_pp:.1f}pp, exceeds "
                        f"{self.fix_rate_drop_threshold_pp:g}pp threshold)"
                    ),
                )
            )

        reg_rise_pp = (
            current.get("regression_rate", 0.0) - baseline.get("regression_rate", 0.0)
        ) * 100
        if reg_rise_pp > self.regression_rate_rise_threshold_pp:
            issues.append(
                RegressionIssue(
                    metric="regression_rate",
                    baseline=float(baseline.get("regression_rate", 0.0)),
                    current=float(current.get("regression_rate", 0.0)),
                    delta_pp=round(reg_rise_pp, 2),
                    threshold_pp=self.regression_rate_rise_threshold_pp,
                    message=(
                        f"Regression rate increased: {baseline.get('regression_rate', 0):.1%} → "
                        f"{current.get('regression_rate', 0):.1%} "
                        f"(+{reg_rise_pp:.1f}pp, exceeds "
                        f"{self.regression_rate_rise_threshold_pp:g}pp threshold)"
                    ),
                )
            )

        return RegressionCheckResult(
            passed=not issues,
            issues=issues,
            current_summary=current,
            baseline_summary=baseline,
        )

    def format_check_result(self, result: RegressionCheckResult | RegressionDetected) -> str:
        """生成 Markdown 格式的回归检查报告。"""
        if isinstance(result, RegressionCheckResult):
            passed = result.passed
            issues = result.issues
            current = result.current_summary
            baseline = result.baseline_summary
        else:
            passed = not result.detected
            issues = result.issues
            current = result.current_summary
            baseline = result.baseline_summary

        lines = ["# Regression Check", ""]
        if passed:
            lines.extend(
                [
                    "**Status**: PASSED",
                    "",
                    "| metric | baseline | current |",
                    "| --- | --- | --- |",
                    f"| fix_rate | {baseline.get('fix_rate', 0):.2%} | "
                    f"{current.get('fix_rate', 0):.2%} |",
                    f"| regression_rate | {baseline.get('regression_rate', 0):.2%} | "
                    f"{current.get('regression_rate', 0):.2%} |",
                ]
            )
            return "\n".join(lines) + "\n"

        lines.extend(["**Status**: FAILED", "", "## Issues", ""])
        for issue in issues:
            lines.append(f"- {issue.message}")
        lines.extend(
            [
                "",
                "## Summary",
                "",
                "| metric | baseline | current | delta | threshold |",
                "| --- | --- | --- | --- | --- |",
            ]
        )
        for issue in issues:
            b = f"{issue.baseline:.2%}" if issue.metric != "avg_retries" else str(issue.baseline)
            c = f"{issue.current:.2%}" if issue.metric != "avg_retries" else str(issue.current)
            sign = "+" if issue.metric == "regression_rate" else "-"
            lines.append(
                f"| {issue.metric} | {b} | {c} | {sign}{issue.delta_pp:.1f}pp | "
                f"{issue.threshold_pp:g}pp |"
            )
        return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="src.eval.regression_check",
        description="对比评测报告与基线，fix_rate 下降 >5pp 或 regression_rate 上升 >3pp 则失败",
    )
    parser.add_argument(
        "--current",
        default="eval_results/eval_report.json",
        help="当前 eval_report.json 或 ablation_report.json",
    )
    parser.add_argument(
        "--baseline",
        default="src/eval/ci_baseline_report.json",
        help="基线报告 JSON",
    )
    parser.add_argument(
        "--fix-rate-threshold",
        type=float,
        default=5.0,
        help="fix_rate 允许下降的最大百分点（默认 5）",
    )
    parser.add_argument(
        "--regression-rate-threshold",
        type=float,
        default=3.0,
        help="regression_rate 允许上升的最大百分点（默认 3）",
    )
    parser.add_argument("--markdown", metavar="PATH", help="可选：写入 Markdown 报告")
    args = parser.parse_args(argv)

    checker = RegressionChecker(
        fix_rate_drop_threshold_pp=args.fix_rate_threshold,
        regression_rate_rise_threshold_pp=args.regression_rate_threshold,
    )

    try:
        result = checker.check(args.current, args.baseline)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print(f"regression_check error: {exc}", file=sys.stderr)
        return 2

    md = checker.format_check_result(result)
    print(md, end="")
    if args.markdown:
        out = Path(args.markdown)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(md, encoding="utf-8")
        print(f"Markdown: {out.resolve()}", file=sys.stderr)

    return 0 if result.passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
