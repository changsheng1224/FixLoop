"""Skill recall eval: compare match_skill output to case expected_skill labels."""

from __future__ import annotations

import json
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path

from src.eval.case_io import DEFAULT_CASES_DIR, build_case_issue, load_case_metadata
from src.skills.matcher import match_skill
from src.state import RepairPlan

_NONE_KEY = "__none__"


@dataclass
class SkillEvalRow:
    """Single case skill match result."""

    case_id: str
    expected_skill: str | None
    matched_skill: str | None
    skill_match: bool
    candidates_count: int = 0
    issue_type: str = ""

    def to_dict(self) -> dict:
        return {
            "case_id": self.case_id,
            "expected_skill": self.expected_skill,
            "matched_skill": self.matched_skill,
            "skill_match": self.skill_match,
            "candidates_count": self.candidates_count,
            "issue_type": self.issue_type,
        }


@dataclass
class SkillEvalReport:
    """Aggregated skill recall report."""

    summary: dict = field(default_factory=dict)
    by_skill: dict = field(default_factory=dict)
    cases: list[SkillEvalRow] = field(default_factory=list)
    confusion: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        """JSON shape aligned with ``eval_report.json`` → ``skill_metrics`` field."""
        metrics = {
            "summary": self.summary,
            "by_skill": self.by_skill,
            "cases": [row.to_dict() for row in self.cases],
            "confusion": self.confusion,
        }
        return {"skill_metrics": metrics}


def normalize_skill_label(name: str | None) -> str | None:
    if name is None:
        return None
    text = str(name).strip()
    if not text or text.lower() in ("null", "none"):
        return None
    return text


def _skills_match(expected: str | None, matched: str | None) -> bool:
    return normalize_skill_label(expected) == normalize_skill_label(matched)


def skill_result_fields_from_plan(
    plan: RepairPlan | None,
    meta: dict,
) -> tuple[str | None, str | None, bool, bool]:
    """Derive eval CaseResult skill fields from repair plan (single source of truth)."""
    skill_labeled = "expected_skill" in meta
    expected = normalize_skill_label(meta.get("expected_skill")) if skill_labeled else None
    matched = plan.skill.matched_skill if plan else None
    skill_match = _skills_match(expected, matched) if skill_labeled else False
    return expected, matched, skill_match, skill_labeled


def evaluate_case_row(case_dir: Path) -> SkillEvalRow | None:
    """Evaluate one case directory; None if directory missing."""
    if not case_dir.is_dir():
        return None

    meta = load_case_metadata(case_dir)
    issue = build_case_issue(case_dir, metadata=meta)
    language = str(meta.get("language") or "python")
    matched = match_skill(issue, language=language)
    matched_skill = matched.name if matched else None
    has_label = "expected_skill" in meta
    expected = normalize_skill_label(meta.get("expected_skill")) if has_label else None
    skill_match = _skills_match(expected, matched_skill) if has_label else False

    return SkillEvalRow(
        case_id=str(meta.get("case_id") or case_dir.name),
        expected_skill=expected,
        matched_skill=matched_skill,
        skill_match=skill_match,
        candidates_count=matched.candidates_count if matched else 0,
        issue_type=str(meta.get("issue_type") or ""),
    )


def load_skill_eval_cases(
    cases_dir: str | Path = DEFAULT_CASES_DIR,
    *,
    case_ids: list[str] | None = None,
) -> list[SkillEvalRow]:
    """Load labeled cases with expected_skill from metadata.yaml."""
    root = Path(cases_dir)
    if case_ids is None:
        ids = sorted(p.name for p in root.iterdir() if p.is_dir() and p.name.startswith("case_"))
    else:
        ids = case_ids

    rows: list[SkillEvalRow] = []
    for case_id in ids:
        case_dir = root / case_id
        if "expected_skill" not in load_case_metadata(case_dir):
            continue
        row = evaluate_case_row(case_dir)
        if row is not None:
            rows.append(row)
    return rows


def compute_skill_metrics(rows: list[dict] | list[SkillEvalRow]) -> dict:
    """Compute precision/recall summary from skill eval rows."""
    normalized: list[dict] = []
    for row in rows:
        if isinstance(row, SkillEvalRow):
            normalized.append(row.to_dict())
        else:
            normalized.append(dict(row))

    total = len(normalized)
    if total == 0:
        return {
            "summary": {
                "total": 0,
                "labeled": 0,
                "correct": 0,
                "accuracy": 0.0,
                "macro_recall": 0.0,
                "no_match_count": 0,
            },
            "by_skill": {},
            "cases": [],
            "confusion": {},
        }

    correct = sum(1 for row in normalized if row.get("skill_match"))
    no_match_count = sum(1 for row in normalized if row.get("matched_skill") is None)

    by_expected: dict[str, list[dict]] = defaultdict(list)
    by_predicted: dict[str, list[dict]] = defaultdict(list)
    confusion: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))

    for row in normalized:
        expected = normalize_skill_label(row.get("expected_skill"))
        matched = normalize_skill_label(row.get("matched_skill"))
        exp_key = expected or _NONE_KEY
        pred_key = matched or _NONE_KEY
        by_expected[exp_key].append(row)
        by_predicted[pred_key].append(row)
        confusion[exp_key][pred_key] += 1

    by_skill: dict[str, dict] = {}
    all_skills = sorted(set(by_expected) | set(by_predicted) - {_NONE_KEY})
    recalls: list[float] = []

    for skill in all_skills:
        if skill == _NONE_KEY:
            continue
        expected_rows = by_expected.get(skill, [])
        predicted_rows = by_predicted.get(skill, [])
        tp = sum(
            1 for row in expected_rows if normalize_skill_label(row.get("matched_skill")) == skill
        )
        fn = len(expected_rows) - tp
        fp = sum(
            1 for row in predicted_rows if normalize_skill_label(row.get("expected_skill")) != skill
        )
        support = len(expected_rows)
        recall = tp / support if support else 0.0
        precision = tp / (tp + fp) if (tp + fp) else 0.0
        if support:
            recalls.append(recall)
        by_skill[skill] = {
            "support": support,
            "tp": tp,
            "fp": fp,
            "fn": fn,
            "precision": round(precision, 4),
            "recall": round(recall, 4),
        }

    macro_recall = round(sum(recalls) / len(recalls), 4) if recalls else 0.0

    return {
        "summary": {
            "total": total,
            "labeled": total,
            "correct": correct,
            "accuracy": round(correct / total, 4),
            "macro_recall": macro_recall,
            "no_match_count": no_match_count,
        },
        "by_skill": by_skill,
        "cases": normalized,
        "confusion": {k: dict(v) for k, v in confusion.items()},
    }


def run_skill_eval(
    cases_dir: str | Path = DEFAULT_CASES_DIR,
    *,
    case_ids: list[str] | None = None,
) -> SkillEvalReport:
    """Run skill recall eval over labeled cases."""
    rows = load_skill_eval_cases(cases_dir, case_ids=case_ids)
    metrics = compute_skill_metrics(rows)
    return SkillEvalReport(
        summary=metrics["summary"],
        by_skill=metrics["by_skill"],
        cases=rows,
        confusion=metrics["confusion"],
    )


def skill_metrics_from_case_results(results) -> dict:
    """Build skill_metrics dict from EvalRunner CaseResult list."""
    rows = []
    for result in results:
        if not getattr(result, "skill_labeled", False):
            continue
        rows.append(
            {
                "case_id": result.case_id,
                "expected_skill": result.expected_skill,
                "matched_skill": result.matched_skill,
                "skill_match": result.skill_match,
                "issue_type": getattr(result, "issue_type", ""),
            }
        )
    if not rows:
        return {}
    return compute_skill_metrics(rows)


def format_skill_markdown(report: SkillEvalReport) -> str:
    """Render skill eval report as Markdown."""
    parts = ["# Skill Recall Eval", ""]

    summary = report.summary
    parts.append("## Summary")
    parts.append("| total | correct | accuracy | macro_recall | no_match |")
    parts.append("| --- | --- | --- | --- | --- |")
    parts.append(
        f"| {summary.get('total', 0)} | {summary.get('correct', 0)} | "
        f"{summary.get('accuracy', 0):.2%} | {summary.get('macro_recall', 0):.2%} | "
        f"{summary.get('no_match_count', 0)} |"
    )
    parts.append("")

    if report.by_skill:
        parts.append("## By Skill")
        parts.append("| skill | support | precision | recall | tp | fp | fn |")
        parts.append("| --- | --- | --- | --- | --- | --- | --- |")
        for skill, metrics in sorted(report.by_skill.items()):
            parts.append(
                f"| {skill} | {metrics['support']} | {metrics['precision']:.2%} | "
                f"{metrics['recall']:.2%} | {metrics['tp']} | {metrics['fp']} | {metrics['fn']} |"
            )
        parts.append("")

    if report.cases:
        parts.append("## By Case")
        parts.append("| case_id | expected | matched | match |")
        parts.append("| --- | --- | --- | --- |")
        for row in report.cases:
            mark = "yes" if row.skill_match else "no"
            parts.append(
                f"| {row.case_id} | {row.expected_skill or '-'} | "
                f"{row.matched_skill or '-'} | {mark} |"
            )
        parts.append("")

    return "\n".join(parts).strip() + "\n"


def write_skill_eval_report(
    report: SkillEvalReport,
    report_path: str | Path,
    *,
    markdown_path: str | Path | None = None,
) -> Path:
    """Write JSON report and optional Markdown."""
    out = Path(report_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8")
    if markdown_path is not None:
        Path(markdown_path).write_text(format_skill_markdown(report), encoding="utf-8")
    return out
