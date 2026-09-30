# WSL command sandbox P1 record

Status: **independent P1 backend and lifecycle verified; product profile disabled pending P2/P3**.
Branch: `codex/wsl-command-sandbox-p1`, based on the P0 branch. No ordinary Agent tool or final verifier is routed through this backend yet. No push, merge, global WSL change, or full test suite was performed.

## Implementation

- `agent_runtime/linux_sandbox/` defines a versioned request/result protocol, a fixed Ubuntu 26.04/WSL2 native-ext4 Python profile, and actual bwrap preflight. The preflight checks namespace/PID, mount visibility, private 64 MiB tmpfs, controlled loopback network negative after host positive, environment keys, and WSL interop surfaces. Missing capability fails closed.
- The controller validates IDs, executable, limits and cwd before persisting `planned`; a native trusted helper outside the task workspace writes `running`, starts bwrap, drains both output pipes with a combined 1 MiB cap, and writes `terminal` only after cleanup. Target stdin is `/dev/null`; control and registry descriptors are not inherited. Controller loss closes the control pipe; supervisor loss relies on bwrap parent-death/PID-namespace behavior, verified below.
- Atomic checksummed registry/receipts live in the external state root; per-workspace `flock` rejects competing executions. Call IDs cannot be reused. Reconciliation checks policy and saved supervisor/target process identity, never signals a stale PID, and rejects nonterminal, corrupt, policy-mismatched or cleanup-unverified state. Unknown side effects are not replayed.
- The toolchain and standard library are narrow read-only mounts; the writable workspace is dedicated, credential-like assets are rejected using the existing sensitive-path policy, `.git` is hidden, and `/tmp` and `/home/sandbox` share the private 64 MiB cap. Policy digest covers WSL boot/distribution, workspace identity, source/profile limits and the mounted toolchain closure.

The helper, `path_safety.py` and `sensitive_paths.py` were mechanically staged under `/home/haoyu/fixloop-sandbox-p0/controller/agent_runtime/`; the source of truth remains this branch. Tests create their own temporary workspace and state directories under the P0 native parent and remove them afterward. The P0 diagnostic remains separate and is not a production controller.

## Verification

- Windows protocol tests: `python -m pytest tests/test_linux_sandbox_protocol.py -q -p no:cacheprovider` -> **3 passed**.
- WSL native toolchain, no repository conftest: `FIXLOOP_P1_ROOT=/home/haoyu/fixloop-sandbox-p0 FIXLOOP_P1_CONTROLLER_ROOT=/home/haoyu/fixloop-sandbox-p0/controller /home/haoyu/fixloop-sandbox-p0/toolchain/bin/python -m pytest --noconftest -c /dev/null -o addopts= -p no:cacheprovider -q <checkout>/tests/test_linux_sandbox_integration.py <checkout>/tests/test_linux_sandbox_lifecycle.py` -> **18 passed** in one combined run (12 integration, 6 lifecycle). Set both variables explicitly in WSL. The test bootstrap imports only the staged trusted sandbox package because the P0 pytest venv intentionally lacks the rest of FixLoop's dependencies.
- `ruff check` and `ruff format --check` on the new package and three tests passed. Full suite was not authorized or run.

Observed scenarios: workspace write and fixed pytest; toolchain EROFS; hidden external state and WSL paths; output hard stop; 64 MiB tmpfs ENOSPC; timeout/cancel with detached child; double fork and inherited output pipe; controller/supervisor SIGKILL with stopped heartbeat; same-workspace lock and other-workspace survival; missing bwrap/toolchain; corrupt receipt, changed process identity and duplicate call ID. All tests use harmless temporary files and no real credentials. This covers the P1 portions of S1/S6-S9/S12; the full S1-S13 matrix is P4.

## Remaining gates

- `run_shell`, `quick_test`, final/pre/post pytest and all other reachable project execution paths are **not yet routed** (P2). A result or receipt here does not prove Agent tasks are sandboxed.
- Session/checkpoint/Observation state still has in-workspace paths in the ordinary runtime. The profile must not be enabled for ordinary tasks before external `state_root` integration (P3).
- On a target write hitting tmpfs ENOSPC, the kernel limit is verified, and the result preserves nonzero exit and stderr; no authoritative `temp_limit_exceeded` classification is claimed from untrusted stderr. CPU/memory/PID hard quotas, kernel escape defense and general multi-distro support remain outside MVP.
- A killed supervisor can leave a nonterminal registry even when parent-death cleanup stops the target. Reconciliation intentionally blocks that workspace pending explicit inspection; it does not guess the side effects or silently retry.
