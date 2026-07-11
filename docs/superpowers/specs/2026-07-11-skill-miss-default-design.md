# Skill Miss Default Strategy — Design Spec

**Date:** 2026-07-11  
**Bonus ref:** docs/bonus.md §13.6  
**Status:** Implemented (Scheme C)

## Goal

When `match_skill()` returns no hit, apply an explicit fallback:
trace metadata, optional generic user hints, and patcher variant routing.

## Routing (Scheme C)

| Condition | `fallback_strategy` | patcher suffix | user hint |
|-----------|---------------------|----------------|-----------|
| `matched_skill` set | `hit` | issue_type route | `format_skill_hint` |
| miss + known `issue_type` | `issue_type_routing` | existing (e.g. `type_error`) | `format_skill_miss_hint` |
| miss + `unknown` / `""` / `test_failure` | `generic_patcher` | `default.txt` | `format_skill_miss_hint` |

## Modules

- `src/skills/fallback.py` — `resolve_skill_fallback`, `apply_skill_fallback`, trace payload
- `src/skills/prompt.py` — `format_skill_miss_hint`, `format_skill_hint_for_plan`
- `src/prompts/skill_miss/{localizer,retriever,patcher}.txt`
- `src/repair/pipeline.py` — apply fallback after `_match_skill`
- `RepairPlan.skill_fallback_strategy`

## Trace (`skill_matched` event)

Miss:

```json
{
  "matched_skill": null,
  "fallback_strategy": "issue_type_routing",
  "patcher_variant": "type_error",
  "inject_miss_hint": true
}
```

Hit: existing `matched.to_trace_payload()` + `"fallback_strategy": "hit"`.

## Tests

```bash
pytest tests/test_skill_fallback.py tests/test_skill_prompt_roles.py -v
```
