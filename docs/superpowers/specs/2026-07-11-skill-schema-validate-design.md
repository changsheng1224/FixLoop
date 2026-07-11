# Skill YAML Schema Validation — Design

**Branch:** `V1.2-Bonus4-skill`  
**Bonus ref:** `docs/bonus.md` §13.1 (enhancement)

## Goal

Layered validation: pydantic L1 (single file) + semantic L2 (catalog) + CLI for CI.

## Layers

| Layer | Module | Rules |
|-------|--------|-------|
| L1 | `SkillSpec` | slug name, language enum, regex, priority 0–100, guidance≥1, known tools |
| L2 | `validate.py` | duplicate name (error), filename≠name (warn), empty example fields (warn), duplicate trigger (warn) |
| L3 | `src.cli skills validate` | print report, exit 1 on errors |

## CLI

```bash
python -m src.cli skills validate [--path DIR]
```

## Acceptance

- `pytest tests/test_skill_validate.py tests/test_skill_matcher.py -v`
