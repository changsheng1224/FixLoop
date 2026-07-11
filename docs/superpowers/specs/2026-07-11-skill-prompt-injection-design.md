# Skill Prompt Injection — Role Variants Design

**Branch:** `V1.2-Bonus4-skill`

## Goal

Inject matched Skill into Localizer, Retriever, and Patcher L2 user task templates with role-specific projections.

## API

`format_skill_hint(plan, role)` where role ∈ `localizer` | `retriever` | `patcher`.

| Role | Content | Char limit |
|------|---------|------------|
| localizer | tool chain + first guidance line | 400 |
| retriever | tool chain + up to 2 guidance bullets | 600 |
| patcher | full block (guidance, avoid, examples) | 1200 |

## Templates

- `localizer.md`, `retriever_*.md`, `patcher.md` — `$skill_hint_block` prefix

## Acceptance

- `pytest tests/test_skill_prompt_roles.py tests/test_skill_matcher.py -v`
