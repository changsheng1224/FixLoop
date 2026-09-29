# Code exploration MVP: real Agent runs

Runs: 72. Each run used a fresh fixture repository and Agent session.
Location mention is a coarse answer screen, not verified correctness.
Repair success requires the fixture's targeted test to exit with code 0.

| Mode | Task | Runs | Path mention | Verified repairs | Mean elapsed ms |
|---|---|---:|---:|---:|---:|
| text | error_definition | 3 | 3 | n/a | 5517 |
| text | same_name | 3 | 3 | n/a | 4255 |
| text | alias_reference | 3 | 3 | n/a | 4675 |
| text | cross_file_import | 3 | 3 | n/a | 4987 |
| text | test_relation | 3 | 3 | n/a | 3453 |
| text | dynamic_unknown | 3 | 3 | n/a | 9559 |
| text | repeat_exploration | 3 | 3 | n/a | 5654 |
| text | repair_import | 3 | n/a | 3 | 6820 |
| lsp | error_definition | 3 | 3 | n/a | 5848 |
| lsp | same_name | 3 | 3 | n/a | 3695 |
| lsp | alias_reference | 3 | 3 | n/a | 7673 |
| lsp | cross_file_import | 3 | 3 | n/a | 4838 |
| lsp | test_relation | 3 | 3 | n/a | 5931 |
| lsp | dynamic_unknown | 3 | 3 | n/a | 10705 |
| lsp | repeat_exploration | 3 | 3 | n/a | 5978 |
| lsp | repair_import | 3 | n/a | 3 | 7312 |
| relations | error_definition | 3 | 3 | n/a | 8697 |
| relations | same_name | 3 | 3 | n/a | 6754 |
| relations | alias_reference | 3 | 3 | n/a | 10192 |
| relations | cross_file_import | 3 | 3 | n/a | 8591 |
| relations | test_relation | 3 | 3 | n/a | 5124 |
| relations | dynamic_unknown | 3 | 3 | n/a | 10531 |
| relations | repeat_exploration | 3 | 3 | n/a | 7908 |
| relations | repair_import | 3 | n/a | 3 | 6683 |

## Model and tool use

Read `per_task_results.jsonl` for per-run token usage, tool calls, session ID, source hashes and trace references.
Review traces before attributing an answer to LSP or the relation view.
The deterministic contract report is in the separate offline run.

## Execution errors

- None recorded.

## Measured comparison and limits

| Mode | Runs | Mean elapsed | Model calls | Input tokens | Output tokens | Cache read tokens | Tool calls |
|---|---:|---:|---:|---:|---:|---:|---:|
| text | 24 | 5,615 ms | 99 | 64,692 | 18,199 | 290,688 | 128 |
| lsp | 24 | 6,497 ms | 95 | 60,146 | 17,429 | 278,144 | 124 |
| relations | 24 | 8,060 ms | 104 | 68,807 | 18,529 | 306,304 | 139 |

All 9 `repair_import` attempts produced the same one-line import correction, and all 9 targeted `test_app.py` runs exited 0. The patch, tool Observation IDs and verification output are recorded per attempt. For example, `evidence/text-repair_import-1.patch` follows source reads in `traces/text-repair_import-1.json`.

The 63 non-repair rows passed a **path-mention proxy**: the answer mentioned the oracle target file. This is not a validated correctness rate. Sample review of the same-name, test-import and dynamic-call answers found appropriate distinctions, but the full set has not been independently judged for semantic accuracy.

`per_task_results.jsonl` is the immutable run output. In that original file, the `correctness` field for non-repair rows was computed from path mention. `scored_results.jsonl` preserves every run and reclassifies that field as `location_mention_proxy`, leaving `correctness` null until semantic review. Reporting semantics were corrected after the run; the manifest's source hashes remain the snapshot from execution time.

Actual tool use limits the comparison: the LSP group made 6 successful `code_lookup` calls across 24 runs. The relations group made 17 `code_lookup` calls but **zero `code_relations` calls**. Therefore this experiment does not establish a relation-view benefit. Mean elapsed time was higher for relations in this sample; the fixture set and three repetitions per task are too small to generalize a performance regression. The semantic memory model was unavailable locally and fell back to keyword mode in all groups.

Deterministic contract results and cold/warm tool timings are in `../offline-final-2026-09-30/report.md`. Those probes confirmed the view can produce observed import/reference edges, but they were scripted and are not Agent outcomes.
