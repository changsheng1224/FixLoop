# Code Exploration P0 baseline

Captured on 2026-09-30 before any changes to the existing file/text tools.

- Git HEAD: `45c60d6977b3bc2b5db0f3676ff1ffc2c2e7f3ec` on `codex/p0-code-exploration-baseline`, equal to local `master` at start.
- Start state: no tracked modifications; many pre-existing untracked files. None were reset, stashed, or overwritten. The generated manifest records a later dirty count that also includes P0 files.
- Existing tool source hashes, fixture hashes, Python/pytest/ruff versions, fixed budget defaults, and the actual `pylsp` path/version are in the versioned `docs/superpowers/evidence/code-exploration-p0/run_manifest.json`. The CLI also writes a local copy under `eval_results/code_exploration_mvp/p0-baseline-2026-09-30/`.
- `pylsp` comes from `C:\Users\haoyu\anaconda3\Scripts\pylsp.exe` and runs with Anaconda Python 3.13.9, `python-lsp-server` 1.13.1, and Jedi 0.19.2. The command-line Python used for P0 is 3.14.3. No installation was needed.
- The eight task inputs are in `tests/fixtures/code_exploration/tasks.json`; scoring data is isolated in `oracles.json`. `repair_import` adapts a fragment from this repository's `src/eval/cases/case_004/repo` at the recorded HEAD.
- The deterministic smoke uses the **original** `list_files`, `grep`, and `read_file` implementations in a fresh temporary Git repository. It located `normalize.py:1` with three calls. Its trace and result are also versioned under `docs/superpowers/evidence/code-exploration-p0/`. This is infrastructure evidence, not a model comparison or a correctness score.
- P4 text/LSP/relations comparisons will all use the future bounded text backend. The P0 record must not be mixed into those T/L/R effect estimates.
- Real model runs remain pending explicit authorization. Real LSP definition/reference integration is a P2 gate; a version check does not satisfy it.

The archived P0 evidence is immutable. After P1 changes the text tools, this command produces a `current_text_snapshot` in `text_deterministic` mode; it cannot reproduce the original pre-I/O baseline from the changed source tree:

```text
python -m src.eval.code_exploration --suite tests/fixtures/code_exploration/tasks.json --deterministic --output eval_results/code_exploration_mvp/current-text
```
