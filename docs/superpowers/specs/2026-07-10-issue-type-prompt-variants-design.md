# Issue-Type Prompt Variants — Design Spec

**Date:** 2026-07-10  
**Status:** Implemented (Scheme A — Patcher only)  
**Refs:** `docs/bonus.md` §5.2 P2, §22.3 P1

## Problem

`patcher.txt` bundled all issue-type guidance (~45 lines) into one system prompt. `_patcher_prompt()` duplicated the same hints in the user message. Patcher uses `complete_once()` with a fixed `_system_prompt` at Agent init, so variants could not be selected per repair.

## Solution (Scheme A)

1. **Split prompts:** `patcher.txt` (base) + `patcher_suffix/{issue_type}.txt`
2. **`load_role_prompt("patcher", issue_type)`** — base + suffix, fallback `default.txt`
3. **`Agent.complete_once(..., system_prompt=...)`** — per-call system override
4. **Orchestrator** — load variant before patch; trace `prompt_variant`
5. **User prompt** — keep task context only (feedback, snippets, allowlist, issue-specific heuristics)

## Suffix files

| File | When used |
|------|-----------|
| `type_error.txt` | TypeError |
| `import_error.txt` | ImportError / ModuleNotFoundError |
| `logic_error.txt` | Eval metadata / manual logic_error |
| `attribute_error.txt` | AttributeError |
| `config_error.txt` | KeyError / pyproject.toml |
| `composite.txt` | Multi-file composite issues |
| `default.txt` | unknown / value_error / syntax_error |

## Out of scope (Phase 2)

- Localizer suffix variants
- LLM issue classifier
- Dynamic agent tool pruning
- Jinja templates

## Verification

```bash
pytest tests/test_prompt_loader.py tests/test_prompts_m5.py tests/test_orchestrator.py -v
```
