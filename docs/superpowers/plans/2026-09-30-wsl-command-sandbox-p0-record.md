# WSL command sandbox P0 record

Status: **P0 diagnostic complete; product profile disabled pending P1-P3**. This is an environment/entrypoint audit and diagnostic, not an implemented sandbox backend. Branch: codex/wsl-command-sandbox-p0; starting commit: 9b1af99f2e6d3fc37183b8a82d7479fe99a648b7. The tracked tree was unchanged at the start; pre-existing untracked files were left untouched. No pull, global WSL configuration change, model call, push, or merge was performed.

## Reproduce

From this Windows checkout, run wsl.exe --list --verbose and wsl.exe --version, then explicitly select the known Ubuntu distribution:

    wsl.exe --distribution Ubuntu --exec python3 /mnt/c/Users/haoyu/Documents/FixLoop/scripts/wsl_sandbox_p0_probe.py

The script is a trusted **diagnostic**, not a controller for untrusted tasks. It uses bounded fixtures under WSL /var/tmp (ext4 here), removes them, and prints JSON. Its first read-only /usr bind is deliberately wider than the production allowlist; a second invocation uses a narrower Python toolchain closure. Do not run a user project through it. Adapt the script path if the checkout moves; never use /mnt/c as the task workspace. The narrow probe requires the provisioned venv below.

## Environment and observations

| Item | Observed |
| --- | --- |
| Windows | 10.0.19045.6466 |
| WSL | 2.7.14.0; Ubuntu selected explicitly; docker-desktop not started |
| Ubuntu | 26.04 LTS; WSL2 kernel 6.18.33.2-microsoft-standard-WSL2 |
| Python | /usr/bin/python3, 3.14.4; pip 25.1.1 |
| pytest | absent from system Python; 9.1.1 in isolated venv |
| bubblewrap | /usr/bin/bwrap, 0.11.1; --size, --die-with-parent, --unshare-pid/net/user available |
| workspace fixture | /var/tmp temporary directory, ext4; /mnt/c is v9fs |

Observed diagnostic results:

- User/PID/network namespaces and a new mount view started; target Python reported PID 2. Workspace writes persisted; the read-only toolchain bind rejected writes with EROFS.
- A temporary external state sentinel and a symlink to it were invisible. /workspace/../state/sentinel, /mnt/c/Windows/System32/cmd.exe, /run/WSL, /proc/sys/fs/binfmt_misc/WSLInterop and hidden /workspace/.git/config were not visible. Outside, cmd.exe and WSLInterop are present. This is not a comprehensive interop escape test.
- A controlled WSL loopback listener accepted a connection outside; inside the new network namespace, connection to that same numeric address and port returned 111 (refused). No DNS or external service was used.
- --size 1048576 --tmpfs /tmp reported 1,048,576 bytes; a 2 MiB write failed with ENOSPC (errno 28). /home/sandbox symlinks to that same tmpfs in this probe, sharing its cap. Recheck 64 MiB in the final profile.
- A second invocation bound only /usr/bin/python3.14, /usr/lib/python3.14, 18 named shared libraries, the loader and the fixed read-only venv. With pytest plugin autoload disabled, an in-sandbox test passed and could not see the state directory or the hidden .git/config. It is still a diagnostic closure, not proof that every repository dependency is available.
- A 64 MiB tmpfs on that narrow invocation reported 67,108,864 bytes. Writing 65 MiB via /home/sandbox failed with ENOSPC; /home/sandbox and /tmp share the same mount. This checks the proposed cap, not resource containment beyond that tmpfs.
- A bounded detached child wrote a heartbeat. After SIGKILL of the bwrap parent, the heartbeat stopped in the observation interval. This is preliminary; supervisor/controller death, double-fork, pipe holders and namespace identity checks remain P1 tests.
- Target environment contained HOME, PATH, PWD and LC_CTYPE (Python locale initialization). No inherited WSL_INTEROP, WSLENV, proxy or credential variable appeared. Final preflight must check values and inherited fds.

## Fixed MVP profile proposal (not enabled)

Authorized P0 provisioning created /home/haoyu/fixloop-sandbox-p0/{state,task-workspace,toolchain}. The parent, state and empty task-workspace are mode 0700 on ext4. The venv is separate from the task workspace and contains pytest==9.1.1, iniconfig==2.3.0, packaging==26.3, pluggy==1.6.0 and Pygments==2.21.0. System Python was not modified. A fresh env with PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 runs pytest 9.1.1. The venv itself is not a trusted controller/helper; P1 must build those outside the writable task workspace.

Recorded SHA-256: /usr/bin/bwrap = 0abea81db798ebf6b4742ac0664802d97521547a353c2a0dbdc21d76cbbfd2c0; /usr/bin/python3.14 = b8d8288faefdd300201f43fcf00f6f539a27218eeed3a3dff5ab10b9c4c99700; venv pyvenv.cfg = 40e233bec4a5dbe1b89042b8c9999be2a61b81ae43d75e214d7c823dc82f98fb. The eventual policy digest must cover the complete mounted file identities, not just these three examples.

- Backend wsl_bwrap only, one Ubuntu WSL2 distribution, one native ext4 credential-free task checkout, single concurrent command per workspace. Reject DrvFs, /mnt/c task roots, symlink escapes and control state in the checkout.
- Trusted controller/helper/policy outside the checkout; separate external state_root for sessions, checkpoints, Observations, receipts and registry. Do not import from the checkout or mount state_root into the target.
- Pin Python+pytest toolchain closure and explicit plugin allowlist; mount only required files read-only. Disable plugin autoload. Do not install from task content. The diagnostic /usr bind is **not** this profile.
- Fixed writable /workspace; hidden/non-writable .git; minimal /proc and /dev; private /tmp and /home/sandbox sharing 64 MiB hard cap; no /mnt, /run, user home or control state. Clear env; pass only fixed PATH/HOME/TMPDIR/LANG/LC_ALL and necessary Python settings, no interop/socket/agent fd.
- Command/quick_test/final-test defaults 20/60/120 s, trusted maximum 120 s; 1 s TERM grace, 3 s KILL confirmation; 1 MiB combined output hard stop and 16 KiB model excerpt. Do not claim CPU/memory/process-count hard quotas.
- Fail closed on missing dependencies, mapping mismatch, isolation failure or uncertain cleanup; no host/Docker/static fallback in this mode.

Policy digest must include real distribution/boot and workspace mapping identity, toolchain file identities, mounts, environment, limits and policy version. Concrete paths and digest remain **unfrozen until** the trusted native checkout, external state root and toolchain are provisioned.

## Executable entrypoint audit

| Entry | Current behavior and required P2 treatment |
| --- | --- |
| L1 run_shell, quick_test | agent_runtime/tools.py:714,920 launch subprocess directly. Route through backend after validation/approval; fixed pytest argv and validated nodeid. |
| ToolExecutor/context | agent_runtime/tool_executor.py:237,655 defaults execution_tier to host; tool_context.py resolves workspace paths. Inject backend, preserve actual receipt metadata, deny unknown execution categories. |
| L2 final verify | src/orchestrator.py:1436,1482 selects Docker, host or static with fallback; src/repair/verification/verify.py:171,453 runs host pytest/profile steps. New mode must use only BwrapVerifyStrategy or fail. |
| Pre/post tests | src/repair/pipeline.py:47,544,696,924 calls src/eval/runner.py:51 host pytest. Route both through backend. Keep evaluation-only case runner separate from production claims. |
| Docker/build/pip | src/tools/sandbox_tools.py and src/harness/sandbox_verify.py can execute project commands and install. Disable in this profile. |
| Git model tools | src/tools/git_tools.py invokes git subprocess; disable. src/orchestrator.py:1634 also invokes git for target discovery: prove trusted-only or disable. |
| Static/profile verifier | src/repair/verification/verify.py:318 and verification_runner.py:37 can launch programs, not pure static data. Disable or route. |
| Code exploration/LSP | agent_runtime/code_exploration/lsp.py:87 starts a service: disable. code_exploration/io.py:491 launches rg: retain only fixed trusted rg binary/argv/env without repo executable/config, or disable. |
| Files/AST/edit lint | Keep trusted local data processing with path/EditLock checks. agent_runtime/tools.py:364 checks AST syntax, not execution. Verify registered grep before allowing the rg exception. |

Reconcile src/tools/spec.py, manifest.py, composite.py and both registries in P2. A metadata label is not isolation. Audit other optional harness/eval subprocess sites against the actually reachable profile before claiming exhaustive coverage.

## Control state and blockers

agent_runtime/session_store.py:79 stores sessions under checkout .agent/sessions; context_runtime.py:336,863 stores Observations under .agent/observations; src/repair/checkpoint_load.py:32 and src/repair/pipeline.py:1134 put repair state in the checkout. src/orchestrator.py:1099 similarly stores run records. P2 integration must stay gated until P3 moves these plus registry/receipts outside the task checkout. Hiding .agent in the mount does not prevent modification of the underlying writable checkout.

**P1 can start**, using the isolated native directories and fixed Python/pytest diagnostic toolchain. P1 must build a trusted native controller/helper outside the writable task checkout and prove autonomous deadlines, bounded output, controller/supervisor death, setsid/double-fork cleanup, uncertain receipts and reconcile. Before enabling any ordinary task, P3 must route all control state into the external state_root; the current in-checkout paths remain a hard gate. The exact real-task dependency/plugin closure and full policy digest also remain to be frozen. No system-wide WSL configuration was changed. P0's probes do not satisfy S1-S13 or support a production security claim.

Official references reviewed: [bubblewrap README](https://github.com/containers/bubblewrap/blob/main/README.md), [bwrap manual source](https://github.com/containers/bubblewrap/blob/main/bwrap.xml), [Microsoft WSL file-system/interoperability documentation](https://learn.microsoft.com/en-us/windows/wsl/filesystems). The spec's old WSL interop URL redirects to the file-system page. Installed bwrap --help and the real probes, not upstream HEAD alone, determine the available options.

## Baseline source SHA-256

    agent_runtime/tools.py            F76892C63F39EBFA2A543C7074CDF6C3446479D54A57E8258CDC37CABB23E6F2
    agent_runtime/tool_executor.py    6A6471003B9852C9083DA71A349C6EDEEF89C5C8C83D1754C8FAB8B4AD73556D
    agent_runtime/tool_context.py     9C5346F9E05266F722FB634A0DDD4DDA62CCF119B41FA66F0E99BF9DDB581E90
    src/repair/verification/verify.py A89F2DFB081A1F3607973D431DF5D1A2716DEB43A12D96BE25C03DEC6828CB78
    src/orchestrator.py               253050B0BB6B9942749291DCEA2C41AB034E94B5497221FD8D00B3DBCAEE104E
    src/repair/pipeline.py            D83C28EDBCEF4965ABBEBA88D003B90A2578C9B57103579A80871557D4531C55
    src/eval/runner.py                56E02E0A7A0A46D02312A9E2772B98241E8EEE2B2E3371B4D2829DAD0CB65F17
    src/repair_factory.py             C8E57C2B75C66E47A92EC24C3EEECAE7EBCFABCA5D64F37BC74850577C9B734C
