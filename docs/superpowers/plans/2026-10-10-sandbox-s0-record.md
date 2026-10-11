# Unified workspace sandbox — S0 feasibility record

**Decision: GO for the next stage for the tested guarded profile.** The live
Windows/WSL2 run passed **101 of 101 checks**, including an ordinary repository,
a detached worktree, isolation attacks, Python/pytest and cleanup. This is S0
feasibility evidence for a fixed diagnostic profile, not production acceptance.
Existing product gates remain closed; no host fallback was introduced.

Branch: `codex/sandbox-s0-windows-probe`. Changes are limited to new diagnostic
scripts, related tests and records. Existing runtime policy and repair
entrypoints were not changed. Pre-existing untracked files were left alone.
No packages, distributions, global WSL settings, model calls, pushes or merges
were performed.

## Deliverables and reproduction

| Artifact | Purpose |
| --- | --- |
| `scripts/wsl_sandbox_s0_probe.py` | Windows coordinator/Linux worker; disposable real Git repository/worktree fixtures with spaces and Chinese names. Defaults to `--profile guarded`. |
| `scripts/wsl_sandbox_s0_guard.py` | Experimental native Windows broker, private FUSE view and private binfmt restriction; standard library only. |
| `tests/test_sandbox_s0_probe.py` | 32 cases: mapping, typed evidence, failures, WSL2 selection, Git environment and cleanup. |
| `tests/test_sandbox_s0_guard.py` | 30 cases: path ambiguity, native identities, hardlink races, mutation failure, transport and FUSE protocol. |
| `2026-10-10-sandbox-s0-guarded-probe.json` | Guarded run: 101 checks, zero blockers, `go_for_next_stage`. |
| `2026-10-10-sandbox-s0-probe.json` | Preserved raw baseline: 73 checks, 62 passed, 11 failed/inconclusive, NO-GO. |

From the repository root, with the existing trusted Python 3.14/pytest venv in WSL:

```powershell
python scripts/wsl_sandbox_s0_probe.py `
  --distribution Ubuntu `
  --workspace-parent C:/Users/haoyu/Documents/FixLoop `
  --toolchain /home/haoyu/fixloop-sandbox-p0/toolchain `
  --profile guarded `
  --output docs/superpowers/plans/2026-10-10-sandbox-s0-guarded-probe.json
```

Use `--profile raw` with a different output filename to reproduce the unsafe
direct-DrvFS profile. Preserve the original baseline report. The toolchain path
is a machine-specific prerequisite, not a product default; bootstrap and the
out-of-box experience are subsequent stages.

The probe creates/removes only `.fixloop-s0-*` fixtures and does not run user
project code. Credential/external-state markers are artificial. Fixture Git
operations clear inherited `GIT_*` variables, disable hooks and use an empty
template. Cleanup verifies the fixture root, handles read-only Git objects and
does not traverse junction targets. The final run left no fixture directories.
Exit 0 requires every required check to pass. Missing/unsupported evidence
produces NO-GO rather than a successful skip.

## Observed environment

| Component | Observed |
| --- | --- |
| Windows | 10.0.19045 |
| Distribution | Existing Ubuntu WSL2, explicitly selected; Docker is not used |
| Kernel / architecture | 6.18.40.1-microsoft-standard-WSL2 / x86_64 |
| Underlying Windows workspace filesystem | v9fs (DrvFS) |
| Guarded workspace view | FUSE; `stat -f` reports `fuseblk` |
| bubblewrap / Python | 0.11.1 / 3.14.4 |
| Toolchain | Existing external Python/pytest venv, bound read-only |

## Resolved blockers and boundary

The raw baseline showed that exact pathname masks were bypassed by `.ENV`,
`.AGENT`, repository `.GIT/config`, and hardlinks to protected/external files.
Hiding `/init`, sockets and environment variables did not prevent the WSL binfmt
interpreter from starting. Its incidental handler failure was inconclusive,
not an enforced execution denial.

The guarded target receives a FUSE view instead of raw DrvFS. A trusted Linux
server outside bwrap forwards bounded, serialized stdio RPC to the native Windows
broker. Neither the channel nor its descriptors reaches the target. Operations
affect the original Windows repository/worktree directly, without whole-workspace
copying or synchronization.

The broker rejects protected names case-insensitively at every depth, ambiguous
Windows spellings, ADS, traversal and private staging names. It checks actual
canonical handle names, reparse attributes and native link counts. Existing
multiply linked files are denied. Parent directory handles are pinned against
rename. `NtCreateFile` resolves one component relative to a checked directory
handle; directory enumeration and rename also use handles, avoiding a second
string-path resolution after the parent check.

Windows share modes and oplocks did not prevent concurrent hardlink creation.
Data mutations therefore use per-file atomic replacement in the same directory.
A private staging inode is set delete-pending before data writes. Link-count
checks reject an alias introduced beforehand, and Windows rejects new links
while it is pending. The broker copies the original file, applies the mutation,
flushes, then clears delete-pending and replaces the allowed entry. No data writes
occur after clearing delete-pending. A link raced into the source retains the
original bytes; a failed write removes staging and preserves the source. This
copies one file per mutation, not the workspace.

The worker starts in fresh user/mount namespaces, mounts a new empty
`binfmt_misc` instance, checks its registrations and disables it. bwrap disables
nested user namespaces, clears the environment, drops capabilities and isolates
PID/network/IPC/UTS. Only the fixed runtime/toolchain closure is exposed. No global
distro interop settings are modified. A read-only Windows executable used solely
as an attack probe returns **errno 8 / ENOEXEC** inside; the outside positive
control succeeds.

The added `stat -f` check exposed a FUSE STATFS reply missing 16 reserved bytes.
A failing protocol regression reproduced it. The corrected 80-byte reply and
final live filesystem-type check passed; the check was not excluded.

## Live results

The 101 checks include setup and positive controls, not 101 independent security
properties. Both workspaces passed every scenario below. Full observations and
errno values are in the guarded JSON report.

| Scenario | Repository | Worktree |
| --- | --- | --- |
| FUSE workspace, read/write persistence, spaces/Chinese names | Pass | Pass |
| Python/pytest in the same mapped workspace | Pass | Pass |
| Listing, rename to new name, delete, mkdir/nested file/rmdir | Pass | Pass |
| Protected names and case aliases reject reads/writes | Pass | Pass |
| Protected/external pre-existing hardlinks reject reads/writes | Pass | Pass |
| Junction, Linux symlink and parent/proc path escapes blocked | Pass | Pass |
| Link/symlink creation, protected rename, ADS and chmod denied | Pass | Pass |
| Protected `.env`/`.agent`/`.git`, external/control markers unchanged | Pass | Pass |
| Windows PE execution denied with ENOEXEC | Pass | Pass |
| Nested user namespace creation denied (errno 28) | Pass | Pass |
| Clean environment/fds, private PID, host paths hidden | Pass | Pass |
| Loopback reachable outside, blocked inside | Pass | Pass |
| Toolchain read-only and private tmpfs capacity 1 MiB | Pass | Pass |

Private binfmt restriction, FUSE worker/mount cleanup and Windows fixture cleanup
also passed. `blockers` is empty. The raw profile remains NO-GO.

## Validation and remaining scope

**62 distinct related regression cases passed** across development batches:
32 probe cases and 30 guard cases. After the native handle rewrite, all then
existing 28 guard cases passed; the added STATFS and write-size regressions passed
separately. The file-size change only rejects writes crossing the existing limit;
the normal live inputs remain covered by the successful run. Targeted Ruff lint
and formatting checks passed. No full repository suite was run.

```powershell
# Reproduce related tests with a fresh disposable pytest base directory.
python -m pytest tests/test_sandbox_s0_probe.py tests/test_sandbox_s0_guard.py `
  -q -p no:cacheprovider --basetemp=.pytest-sandbox-s0-reproduce
ruff check --no-force-exclude scripts/wsl_sandbox_s0_probe.py `
  scripts/wsl_sandbox_s0_guard.py tests/test_sandbox_s0_probe.py tests/test_sandbox_s0_guard.py
```

This prototype supports a fixed Python diagnostic profile. Workspace executables,
hardlinks, symlinks, xattrs, chmod and special-file creation are denied; user rename
over an existing destination is unsupported. Data mutations copy the file, with
a 256 MiB file limit and 64 KiB RPC I/O limit. Atomic replacement changes inode
identity and does not preserve full ACL/attribute/POSIX metadata semantics; those
need production design and testing. This is not yet a general build environment.

Managed-distribution bootstrap, session lifecycle, full tool routing, approval
policy, CPU/memory/process quotas, cancellation/recovery, network egress policy
and performance remain subsequent-stage work. This run does not prove exhaustive
reparse/race coverage or behavior on every Windows/WSL version. Keep production
Windows-path rejection and sandbox/repair gates until the production replacement
profile is implemented and accepted; unavailable isolation must never silently
fall back to host execution.
