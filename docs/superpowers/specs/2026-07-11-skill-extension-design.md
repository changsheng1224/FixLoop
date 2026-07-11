# Skill Extension — Phase 1 Design

**Branch:** `V1.2-Bonus4-skill`  
**Bonus ref:** `docs/bonus.md` §13.1–13.3, §23  
**Layer:** L2 (`src/skills/`, `src/repair/`, `src/prompts/`)

## Goal

Extend existing YAML Skill system with pydantic validation, deterministic matching, patcher prompt injection, and trace observability.

## Architecture

```
src/skills/
  models.py    SkillSpec (pydantic), MatchedSkill
  catalog.py   SkillCatalog.load_from_directory, get_default_catalog()
  matcher.py   match_skill(issue, language) → MatchedSkill | None
  prompt.py    format_skill_hint_block(RepairPlan) → [Skill 提示]

pipeline.py    match → RepairPlan fields + trace skill_matched
patcher.md     $skill_hint_block in user template
```

## Matching

1. Filter by `language`
2. Collect regex hits via `re.search(trigger_pattern, issue)`
3. Sort: `priority` DESC → `len(trigger_pattern)` DESC → `name` ASC
4. Return top-1 or `None`

## RepairPlan fields

- `matched_skill: str | None`
- `suggested_tools: list[str]`
- `skill_example_patch: str`

Stop writing `suggested_tools` into `estimated_impact`.

## Trace

Event `skill_matched` with `matched_skill`, `trigger_pattern`, `priority`, `candidates_count`; null payload when no match.

## Acceptance

- `pytest tests/test_skill_matcher.py -v`
- PR前 `pytest tests/ -v`

### YAML schema

- `example_issue` — 泛化参考 issue（非 eval 专用路径）
- `guidance` — 3–5 条修复原则
- `avoid` — 1–3 条反模式
- `example_patch` — 示意性补丁片段

## Built-in catalog

10 Python skills covering all eval `issue_type` values (001–010).

## Out of scope (Phase 2)

Multi-source catalog, Gateway warn, eval skill_metrics, vector retrieval.
