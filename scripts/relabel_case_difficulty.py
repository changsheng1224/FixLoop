#!/usr/bin/env python3
"""难度重标定：读 eval_report.json → 按 fix_rate 重新标定 difficulty → 写回 metadata.yaml。

用法:
    python scripts/relabel_case_difficulty.py [eval_report.json] [--cases-dir src/eval/cases/] [--dry-run]
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import yaml

_PROJECT = Path(__file__).resolve().parent.parent
if str(_PROJECT) not in sys.path:
    sys.path.insert(0, str(_PROJECT))


def compute_difficulty(fix_rate: float) -> str:
    if fix_rate >= 0.9:
        return "easy"
    elif fix_rate >= 0.6:
        return "medium"
    return "hard"


def relabel(
    report_path: Path,
    cases_dir: Path,
    *,
    dry_run: bool = True,
) -> dict[str, str]:
    """读 eval 报告，按 fix_rate 重新标定难度，写回 metadata.yaml。"""
    report = json.loads(report_path.read_text(encoding="utf-8"))
    changes: dict[str, str] = {}

    by_case = report.get("by_case") or report.get("cases") or {}
    if not by_case:
        # 尝试从 details 提取
        for variant_data in report.values():
            if isinstance(variant_data, dict) and "details" in variant_data:
                for detail in variant_data["details"]:
                    cid = detail.get("case_id", "")
                    if cid and cid not in changes:
                        fr = detail.get("fix_rate", 0) or 0
                        changes[cid] = compute_difficulty(fr)
        if not changes:
            print("No case-level data found in report")
            return {}

    for cid in (by_case or changes):
        case_dir = cases_dir / cid
        if not case_dir.is_dir():
            continue
        meta_path = case_dir / "metadata.yaml"
        if not meta_path.is_file():
            continue

        meta = yaml.safe_load(meta_path.read_text(encoding="utf-8")) or {}
        old_diff = meta.get("difficulty", "unknown")

        if cid in by_case:
            case_data = by_case[cid]
            fr = case_data.get("fix_rate", 0) if isinstance(case_data, dict) else 0
        else:
            fr = 0

        new_diff = compute_difficulty(fr)
        if new_diff == old_diff:
            continue

        changes[cid] = f"{old_diff} → {new_diff}"
        if not dry_run:
            meta["difficulty"] = new_diff
            meta_path.write_text(yaml.dump(meta, allow_unicode=True), encoding="utf-8")

    return changes


def main() -> int:
    report_path = Path(sys.argv[1]) if len(sys.argv) > 1 else _PROJECT / "eval_results" / "eval_report.json"
    cases_dir = _PROJECT / "src" / "eval" / "cases"
    dry_run = "--dry-run" in sys.argv or "-n" in sys.argv

    if not report_path.is_file():
        print(f"Report not found: {report_path}")
        return 1

    print(f"{'[DRY RUN] ' if dry_run else ''}Relabeling from {report_path}")
    changes = relabel(report_path, cases_dir, dry_run=dry_run)

    if not changes:
        print("No changes needed.")
        return 0

    for cid, ch in sorted(changes.items()):
        print(f"  {cid}: {ch}")
    print(f"{len(changes)} case(s) would be relabeled." if dry_run else f"{len(changes)} case(s) relabeled.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
