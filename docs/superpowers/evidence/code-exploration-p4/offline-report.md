# Code exploration MVP evaluation

Runs: 24. Evaluation: deterministic contract only.

| Mode | Task | Repetitions | Successful runs | Definition checks | Relation checks |
|---|---|---:|---:|---:|---:|
| text | error_definition | 1 | n/a | 1 | 0 |
| text | same_name | 1 | n/a | 1 | 0 |
| text | alias_reference | 1 | n/a | 1 | 0 |
| text | cross_file_import | 1 | n/a | 1 | 0 |
| text | test_relation | 1 | n/a | 1 | 0 |
| text | dynamic_unknown | 1 | n/a | 1 | 0 |
| text | repeat_exploration | 1 | n/a | 1 | 0 |
| text | repair_import | 1 | n/a | 1 | 0 |
| lsp | error_definition | 1 | n/a | 1 | 0 |
| lsp | same_name | 1 | n/a | 1 | 0 |
| lsp | alias_reference | 1 | n/a | 1 | 0 |
| lsp | cross_file_import | 1 | n/a | 1 | 0 |
| lsp | test_relation | 1 | n/a | 1 | 0 |
| lsp | dynamic_unknown | 1 | n/a | 1 | 0 |
| lsp | repeat_exploration | 1 | n/a | 1 | 0 |
| lsp | repair_import | 1 | n/a | 1 | 0 |
| relations | error_definition | 1 | n/a | 1 | 0 |
| relations | same_name | 1 | n/a | 1 | 0 |
| relations | alias_reference | 1 | n/a | 1 | 1 |
| relations | cross_file_import | 1 | n/a | 1 | 1 |
| relations | test_relation | 1 | n/a | 1 | 1 |
| relations | dynamic_unknown | 1 | n/a | 1 | 0 |
| relations | repeat_exploration | 1 | n/a | 1 | 0 |
| relations | repair_import | 1 | n/a | 1 | 0 |

Deterministic probes are scripted checks, not Agent effectiveness evidence.
Cold/warm timings are measured within each isolated run; they include tool work only.

| Mode | Mean cold text ms | Mean warm text ms | Mean cold LSP ms | Mean warm LSP ms |
|---|---:|---:|---:|---:|
| text | 28 | 23 | n/a | n/a |
| lsp | 22 | 20 | 5646 | 9 |
| relations | 23 | 22 | 5712 | 10 |

## Failed or incomplete checks

- text/alias_reference/#1: references:runner.py:operations.py
- text/cross_file_import/#1: imports:service.py:pkg/calc.py
- text/test_relation/#1: test_imports:test_service.py:service.py
- text/repair_import/#1: imports:app.py:utils/helpers.py
- lsp/alias_reference/#1: references:runner.py:operations.py
- lsp/cross_file_import/#1: imports:service.py:pkg/calc.py
- lsp/test_relation/#1: test_imports:test_service.py:service.py
- lsp/repair_import/#1: imports:app.py:utils/helpers.py
- relations/repair_import/#1: imports:app.py:utils/helpers.py

Text and LSP modes do not produce a relation view. The `repair_import` fixture starts with a broken import, so its target relation should remain unresolved until a verified repair is made.
Per-run traces retain coverage, truncation, provenance and degraded LSP results.

Real model comparison and verified repair: pending explicit authorization.
