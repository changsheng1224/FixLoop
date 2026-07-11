# Skill 块注入 Prompt 统一 — 设计规格（Scheme A）

## FixLoop Context

- **Bonus ref:** `docs/bonus.md` §5.3 / §13.3
- **Layer:** L2 (`src/skills/`, `src/prompts/`)
- **Primary modules:** `src/skills/skill_block.py`, `src/skills/prompt.py`, `src/prompts/repair_tasks.py`
- **Acceptance:** `pytest tests/test_skill_block.py tests/test_skill_prompt_roles.py tests/test_skill_fallback.py -v`
- **Branch:** `V1.2-Bonus5-Prompt`

## 目标

单一 `SkillBlockRenderer`：hit/miss × 四角色（含 Verifier）统一 `[Skill 提示]` + `角色:` schema；trace `skill_hint_rendered`。

## 不在范围

- L1 `ContextManager._get_skills()` 动态注入（Scheme C）
- Skill 向量检索 / catalog index
