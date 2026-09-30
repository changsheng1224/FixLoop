# WSL command sandbox P4 record

Date: 2026-09-30. Branch: `codex/wsl-command-sandbox-p4`. Status: **partial**. S13 real Agent repair is pending explicit authorization for model usage/cost. No production profile, push, merge, or full test suite is claimed.

The later local refactor and its own real-machine results are recorded in [the refactor record](2026-09-30-wsl-command-sandbox-refactor-record.md); the P4 evidence below remains the original baseline.

## Reproduction and evidence

The trusted evaluator is `src/eval/sandbox_mvp.py`; its harmless fixture is `tests/fixtures/linux_sandbox/test_fixture.py`. The evaluator accepts only four trusted configuration keys (`workspace`, `state_root`, `toolchain`, `helper`), creates a fresh WSL ext4 workspace and separate state directory per scenario, and never takes model-provided commands. The Windows-to-WSL invocation uses fixed argv with `python3 -I`; the evaluation process explicitly imports the checkout from `/mnt/c`, but the **target** workspace is always native WSL ext4. This evaluation import mechanism is not the product launcher.

Trusted config: `/home/haoyu/fixloop-sandbox-p0/controller/p2-live-config.json`. Results are retained outside the task workspaces:

- Isolation: `/home/haoyu/fixloop-sandbox-p0/p4-isolation-final-v2/` (`cases.jsonl`, `receipts/`, `traces/evaluation.jsonl`, `environment_manifest.json`, `policy.json`, `report.md`). **S1–S12 passed, S13 pending, 0 failed**. Exit status 1 intentionally reflects incomplete coverage.
- Overhead: `/home/haoyu/fixloop-sandbox-p0/p4-overhead-final-v2/` (`overhead.jsonl`, 44 bwrap receipts, manifest, policy, `summary.json`, report). **88/88 successful**: four tasks x two tiers x (one warmup + ten measured). Preflight: 1101.275 ms, separately recorded in manifest. Both final manifests have the same fixture and evaluator SHA-256.

On this Windows host, replay either suite with the fixed evaluator import (replace `isolation`/output directory with `overhead`/a fresh directory, adding `--repetitions 10` for the latter):

```powershell
wsl.exe --distribution Ubuntu --exec /usr/bin/python3 -I -c "import sys,types; root='/mnt/c/Users/haoyu/Documents/FixLoop'; sys.path.insert(0,root); pkg=types.ModuleType('agent_runtime'); pkg.__path__=[root+'/agent_runtime']; sys.modules['agent_runtime']=pkg; from src.eval.sandbox_mvp import main; sys.exit(main(['--config','/home/haoyu/fixloop-sandbox-p0/controller/p2-live-config.json','--suite','isolation','--output','/home/haoyu/fixloop-sandbox-p0/p4-isolation-replay']))"
```

The isolated package shim prevents importing unrelated controller dependencies from the Windows checkout; it is used by the trusted evaluator only, outside the target sandbox.

Each isolation row has a case ID, fixture/evaluator hash, task/run identity, per-call policy digest where execution began, outcome and diagnostic; successful regular calls carry result and receipt checksum. The manifest's policy digest refers only to its preflight workspace. Controller/supervisor death scenarios retain their evidence separately. The `traces/evaluation.jsonl` file is an **evaluation trace**, not a product Canonical Trace. An assertion of `passed` here means the specific scripted probe passed, not that every variant named in the spec's scenario description was exhaustively tested.

## Isolation and demo

1. **Boundary and network**: S2 follows an outward symlink and checks an external path; S3 checks the separate control-state sentinel, home SSH path and Docker socket; S5 checks Windows executable, WSL socket and proc binfmt visibility. S4 first connects to a controlled `127.0.0.1` listener from trusted WSL, then proves the same numeric endpoint is inaccessible from bwrap. Check `cases.jsonl` and `receipts/S2.json` through `S5.json` (S4 includes its own receipt).
2. **Timeout, cancellation and process cleanup**: S6 starts a detached heartbeat writer, times out, then separately cancels a double-fork/setsid writer. Confirmed cleanup is coupled with an unchanged heartbeat after a delay; S7 kills a verified supervisor identity and checks the next call is rejected as uncertain; S12 checks the workspace lock. Inspect `receipts/S6.json`, `S6-cancel.json`, `S12.json`, and the S7 case row. The S7 missing terminal receipt is intentional, not a success receipt.
3. **Partial write and recovery**: S10 forks a controller that writes `partial`, kills it after the write, waits for a terminal cleanup receipt, reconciles, and attempts the same call ID again. The persisted file remains changed and replay is rejected. Inspect `receipts/S10.json` and the S10 case row. This demonstrates no blind replay; it does not imply automatic rollback.

S1 additionally executes fixed Python and pytest with receipts and verifies the toolchain bind is read-only. S8 rejects a missing toolchain without executing a host fallback. S9 exercises output termination and the 64 MiB tmpfs write failure. S11 corrupts a receipt after copying the original into `receipts/` and confirms reconciliation rejects it. P0–P3 records and related tests cover additional protocol/routing/resume paths; P4 does not substitute for a real-model S13 closure.

## Overhead

Same WSL distro/toolchain and fixture, explicit trusted-host Linux baseline (never production fallback). Values are `total_ms` median / interpolated p95 for the ten non-warmup samples:

| Task | Trusted host Linux | WSL bwrap |
| --- | ---: | ---: |
| Python startup | 14.531 / 15.574 | 778.794 / 793.189 |
| Small file write | 27.682 / 28.159 | 796.471 / 814.266 |
| One pytest test | 248.693 / 252.423 | 1009.577 / 1024.082 |
| 4 KiB output | 14.543 / 14.872 | 771.863 / 781.605 |

The per-call bwrap cost is approximately 0.77 s above this host baseline for the small tasks. It includes policy preflight, process startup, execution, receipt and cleanup; `overhead.jsonl` preserves each total, sandbox startup/duration/cleanup breakdown, output bytes, exit/status, cleanup state, digest and diagnostics. The ten-sample p95 is descriptive, not a stable performance estimate. Host rows have no sandbox receipt or phase breakdown by design.

## Boundary and remaining gate

The bwrap policy limits target file view, network and PID namespace, and the external state root is not mounted. It does not OS-sandbox the trusted Python file/AST/snapshot tools, make the task workspace read-only, or defend against kernel/WSL escape, multi-tenant adversaries, CPU/memory/fork exhaustion, or arbitrary dependency installation. Command allowlists alone cannot isolate repository code or pytest/conftest side effects. Unknown cleanup is `uncertain` and blocks further execution; resume checks identity and receipts rather than reissuing an unknown side effect. Native workspace and a credential-free repo are prerequisites.

S13 still needs an authorized real Agent repair on a dedicated repository, with actual `quick_test` and final pytest receipts linked to the patch. Some broad scenario variants (for example every external network class, missing bwrap/namespace variants and independent process-namespace identity cross-checks) are not individually enumerated by the P4 script; do not treat the 12 passed probes as universal security proof. P0–P2 records and P3 tests provide additional context. Related Windows tests: `7 passed, 6 skipped` for the P4/protocol/lifecycle selection; skipped Linux live cases are replaced here by WSL real-machine results. Ruff check/format and `git diff --check` passed. Full suite was not run.
