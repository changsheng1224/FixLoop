# WSL command sandbox P2 record

Status: **P2 gated tool/verification integration verified; ordinary repair remains disabled until P3**. Branch: `codex/wsl-command-sandbox-p2`. No push, merge, full test suite, or production profile enablement was performed.

## Routing

- `ToolContext` can explicitly carry the P1 backend. `run_shell` retains the existing allowlist and argv parser, then permits only Python aliases through the fixed `/toolchain/bin/python` profile. `quick_test` validates the file part of nodeids and uses controller-fixed pytest options. Unsupported executables/options/escaped paths fail closed.
- `ToolExecutor` retains approval/budget and snapshots around sandbox commands, treats them as potentially mutating, preserves structured status and sandbox receipt metadata, never rolls back a sandbox call on cancellation/failure, and blocks further commands/file writes after unverified cleanup. The sandbox registry disables Docker/git model tools and external LSP; manifest roles cannot re-enable disabled tools. Sandbox grep uses bounded Python scanning instead of resolving a host `rg` from PATH. File tools and pure AST remain trusted controller operations, not OS-sandboxed.
- `BwrapVerifyStrategy` maps pytest to passed/failed/environment/interrupted/no-tests. The Orchestrator's Python verifier and baseline/post-patch pytest record use that same backend when explicitly injected, with no Docker/host/static fallback. The test-patch overlay still surrounds final Python verification. A completed exit 0 is not accepted without confirmed cleanup, actual Linux sandbox identity, and a receipt.
- The Windows launcher uses fixed `wsl.exe --distribution Ubuntu --exec <trusted-python> -I <trusted-controller> <trusted-config>` argv. The project request goes over JSON stdin, not through PowerShell, cmd, or shell interpolation. The controller entry and test config are staged under the P0 trusted controller directory, outside the native workspace.
- `--execution-backend wsl_bwrap` and direct full `Orchestrator.repair()` remain fail-closed pending P3 external session/checkpoint/Observation state. Conflicting legacy tier/require-sandbox and LSP flags are rejected before model setup. `skip-verify` is not treated as a verified result.

## Verification

- Windows related tests, including new fake-backend routing, result/receipt, CLI gate, registry, verifier, launcher, and P1 protocol regression: **175 passed, 3 skipped** (live/native fixtures require explicit opt-in).
- WSL native P2 fixture (`tests/test_linux_sandbox_p2_integration.py`, explicit P0 environment): **1 passed**. Actual `run_shell`, `quick_test`, and final pytest each produced a distinct P1 receipt; final receipt reconciled from the external state store. No host fallback was used.
- Windows-to-WSL live fixture (`FIXLOOP_P2_LIVE=1`, `tests/test_wsl_launcher_live.py`): **1 passed**, repeated after a WSL restart with per-run native workspace/state and trusted config. A Python command wrote one harmless test file; the following pytest passed. Both calls reported actual `linux_sandbox`, confirmed cleanup, and distinct receipts. The test verifies its resolved path boundary before deleting its own temporary fixture. Reusing the earlier fixed state across a WSL boot returned `resume_policy_mismatch`, with no command fallback; the old test-only receipt/config remain under the P0 root for forensic inspection and are not mounted into the target.
- `ruff check`, `ruff format --check`, and `git diff --check` on the P2 files; no full test suite.

## Remaining gate

P3 must move all session/checkpoint/Observation/control state out of the workspace, reconcile unknown calls before resuming, and block file writes across all control paths. The P2 test-only injection does not establish a safe end-to-end repair loop. P4 still owes S1–S13 full matrix, overhead measurements, and a real repair closure. CPU/memory/fork limits and general multi-distribution support remain outside this MVP.
