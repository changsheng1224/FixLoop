#!/usr/bin/env python3
"""Badcase → eval Case 晋升：从失败 run 生成 case_XXX 骨架。

用法:
    python scripts/promote_badcase.py <run_id> [--output src/eval/cases/]
    python scripts/promote_badcase.py latest --dry-run

生成:
    case_XXX/
      issue.txt           ← task_state.user_request
      expected_patch.diff ← (人工填写)
      metadata.yaml       ← 从 node_timings/status 推断
      min_lines.txt       ← 1 (人工校准)
      repo/               ← (人工复制)
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

_PROJECT = Path(__file__).resolve().parent.parent
if str(_PROJECT) not in sys.path:
    sys.path.insert(0, str(_PROJECT))

DEFAULT_OUTPUT = _PROJECT / "src" / "eval" / "cases"


def find_run_dirs(runs_root: Path) -> list[Path]:
    if not runs_root.is_dir():
        return []
    return sorted(runs_root.iterdir(), key=lambda p: p.stat().st_mtime_ns, reverse=True)


def load_task_state(run_dir: Path) -> dict | None:
    path = run_dir / "task_state.json"
    if not path.is_file():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None


def load_report(run_dir: Path) -> dict | None:
    path = run_dir / "report.json"
    if not path.is_file():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None


def next_case_id(output_dir: Path) -> str:
    existing = sorted(output_dir.glob("case_*"))
    max_n = 0
    for d in existing:
        try:
            n = int(d.name.split("_")[1])
            max_n = max(max_n, n)
        except (IndexError, ValueError):
            pass
    return f"case_{max_n + 1:03d}"


def generate_case_skeleton(
    run_dir: Path, output_dir: Path, *, dry_run: bool = False
) -> Path | None:
    ts = load_task_state(run_dir)
    report = load_report(run_dir)
    if not ts:
        print(f"task_state.json not found in {run_dir}")
        return None

    case_id = next_case_id(output_dir)
    case_dir = output_dir / case_id

    issue = ts.get("user_request", "") or ts.get("current_goal", "")
    status = ts.get("status", "failed")
    if report:
        status = report.get("status", status)

    issue_type = "unknown"
    il = issue.lower()
    rl = str(report).lower()
    # 先检测精确模式，再 fallback
    type_map = {
        "typeerror": "type_error", "import": "import_error",
        "composite": "composite", "logic": "logic_error",
        "attributeerror": "attribute_error", "config": "config_error",
        "test_failure": "test_failure", "assertion": "test_failure",
    }
    for keyword, itype in type_map.items():
        if keyword in il or keyword in rl:
            issue_type = itype
            break

    meta = {
        "case_id": case_id,
        "issue_type": issue_type,
        "difficulty": "medium",
        "expected_skill": "",
        "description": f"Promoted from {run_dir.name}",
        "tags": [issue_type, "badcase"],
    }

    if dry_run:
        print(f"[DRY RUN] Would create: {case_dir}")
        print(f"  issue_type={issue_type}, status={status}")
        print(f"  issue[:100]={issue[:100]}")
        return None

    case_dir.mkdir(parents=True, exist_ok=True)
    import yaml

    (case_dir / "issue.txt").write_text(issue[:2000], encoding="utf-8")
    (case_dir / "expected_patch.diff").write_text("# TODO: add expected unified diff\n", encoding="utf-8")
    (case_dir / "min_lines.txt").write_text("1", encoding="utf-8")
    (case_dir / "metadata.yaml").write_text(yaml.dump(meta, allow_unicode=True), encoding="utf-8")
    (case_dir / "repo").mkdir(exist_ok=True)

    print(f"Created: {case_dir}")
    print(f"  issue_type={issue_type}, status={status}")
    print(f"  Next: fill expected_patch.diff + copy repo/ files + adjust min_lines.txt")
    return case_dir


def main() -> int:
    import argparse

    p = argparse.ArgumentParser(description="Badcase → eval Case 晋升")
    p.add_argument("run_id", help="Run ID or 'latest'")
    p.add_argument("--output", default=str(DEFAULT_OUTPUT), help=f"Output dir (default: {DEFAULT_OUTPUT})")
    p.add_argument("--dry-run", action="store_true", help="Print what would be created")
    args = p.parse_args()

    runs_root = Path(".agent/runs")
    dirs = find_run_dirs(runs_root)
    if not dirs:
        print("No runs found in .agent/runs/")
        return 1

    run_dir = dirs[0] if args.run_id == "latest" else runs_root / args.run_id
    if not run_dir.is_dir():
        print(f"Run not found: {run_dir}")
        return 1

    output_dir = Path(args.output)
    result = generate_case_skeleton(run_dir, output_dir, dry_run=args.dry_run)
    return 0 if result or args.dry_run else 1


if __name__ == "__main__":
    raise SystemExit(main())
