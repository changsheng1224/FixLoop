# WSL sandbox local refactor record

Date: 2026-09-30. Branch: `codex/wsl-command-sandbox-refactor`, based on P4 commit `00fa749`. This changes only the local, unpushed sandbox line; it does not complete the deferred S13 real-model repair.

## Changes

- `agent_runtime/linux_sandbox/tool_policy.py` is the default-deny tool-access policy shared by the L1 executor and L2 repair registry. Trusted data/control/write and sandbox-command classes are explicit; unknown tools and unaudited execution tiers are rejected. No SWE-bench instance, repository, or expected patch is encoded in the product policy.
- `ToolExecutor` never captures a post-execution workspace diff or attempts a rollback while sandbox cleanup is uncertain. It records `workspace_diff_status=pending_cleanup`, preserves the uncertain status, and blocks subsequent writes/commands. A regression test observed the original extra snapshot and now checks that only the pre-execution snapshot occurs.
- The P4 evaluator dispatches S1–S13 through a scenario registry. Shared probe execution, receipt writing, basic result assertions, and lifecycle setup are centralized; scenario-specific checks remain only in evaluation code. A scenario exception becomes a failed row rather than aborting the matrix.
- Overhead sampling records host timeouts/start failures as individual failed rows. A sandbox result without a confirmed receipt stops further sandbox submissions and marks remaining samples blocked. Statistics use only successful measured samples, report their count, and use `null` instead of a fabricated duration for zero valid samples.

## Verification

- Relevant Windows selection: 119 passed, 18 skipped (Linux live tests are opt-in); executor policy regression selection: 53 passed. Ruff check/format and `git diff --check` passed. Full suite not run.
- Final WSL isolation: `/home/haoyu/fixloop-sandbox-p0/refactor-isolation-final-v2/`: S1–S12 passed, S13 pending, 0 failed; 13 case rows and 14 supporting receipts.
- Final WSL overhead: `/home/haoyu/fixloop-sandbox-p0/refactor-overhead-final-v2/`: 88/88 passed, 44 sandbox receipts, four tasks x two tiers x (one warmup + ten measured). Median `total_ms` host / bwrap: startup 14.880 / 790.520; write 27.876 / 806.309; pytest 251.305 / 1034.778; 4 KiB output 15.195 / 794.408. The two manifests have matching evaluator SHA-256 `4a27ed19da06d639a90a77c94ddc37be59b19541c0edacad52f6f4b9d0a26c40` and matching fixture hash. Original P4 results remain untouched.

These numbers describe this one WSL profile, not a cross-platform performance guarantee. The evaluator trace is not the product Canonical Trace. S13 and full-suite/release validation remain separate gates; no real model call or remote operation was performed.
