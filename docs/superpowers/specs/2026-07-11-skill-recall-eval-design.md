# Skill Recall Eval — Design Spec

**Date:** 2026-07-11  
**Bonus ref:** docs/bonus.md §13.5  
**Status:** Implemented

## Goal

Measure deterministic Skill matcher quality against labeled eval cases:
`expected_skill` (metadata) vs `matched_skill` (`match_skill(issue)`).

## Architecture (B + C hybrid)

### Phase 1 — Standalone eval

- `src/eval/skill_metrics.py` — load cases, compute metrics, Markdown report
- `metadata.yaml` field `expected_skill` on case_001–010
- CLI: `python -m src.cli eval skills --all`
- Output: `skill_eval_report.json` with top-level `skill_metrics` object

### Phase 2 — EvalRunner integration

- `CaseResult`: `expected_skill`, `matched_skill`, `skill_match`, `skill_labeled`
- `EvalRunner.run_case()` calls `evaluate_case_row()` before repair
- `EvalReport.skill_metrics` populated via `skill_metrics_from_case_results()`

## Metrics

| Field | Definition |
|-------|------------|
| accuracy | correct / total labeled cases |
| macro_recall | mean of per-skill recall (skills with support > 0) |
| by_skill | tp/fp/fn, precision, recall per skill name |
| confusion | expected → predicted counts |

Exit code: 0 iff accuracy == 1.0 (standalone `eval skills`).

## Case labels (verified)

| case | expected_skill |
|------|----------------|
| 001–003 | python_type_error_fix |
| 004 | python_import_error_fix |
| 005 | python_cannot_import_name_fix |
| 006 | python_logic_error_fix |
| 007 | python_attribute_error_fix |
| 008 | python_test_failure_fix |
| 009 | python_config_error_fix |
| 010 | python_composite_fix |

## Tests

- `tests/test_skill_eval.py` — unit + integration against real case dir

## Usage

```bash
python -m src.cli eval skills --all --verbose
python -m src.cli eval skills --all --markdown
pytest tests/test_skill_eval.py -v
```
