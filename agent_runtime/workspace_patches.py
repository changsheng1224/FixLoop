"""Host backend: snapshots, optimistic preconditions and atomic patch commits."""

from __future__ import annotations

import hashlib
import threading

from agent_runtime.atomic_io import atomic_write_bytes
from agent_runtime.patch_engine import PatchError

_COMMIT_LOCK = threading.RLock()


class HostPatchTransaction:
    def __init__(self, resolve, paths):
        self.targets = {path: resolve(path) for path in paths}
        if len({str(p.resolve()).casefold() for p in self.targets.values()}) != len(paths):
            raise PatchError("duplicate target")
        self.before = {}
        for path, target in self.targets.items():
            if target.is_symlink() or target.exists() and not target.is_file():
                raise PatchError(f"patch target is not a regular file: {path}")
            self.before[path] = (
                (target.read_bytes(), target.stat().st_mode) if target.exists() else (None, None)
            )

    def sources(self):
        return {
            path: data.decode("utf-8") if data is not None else None
            for path, (data, _) in self.before.items()
        }

    def check_hashes(self, expected):
        if not expected:
            return
        if not isinstance(expected, dict):
            if len(self.before) != 1:
                raise PatchError("multi-file patch requires hashes keyed by path")
            expected = {next(iter(self.before)): expected}
        for path, digest in expected.items():
            if path not in self.before:
                raise PatchError("hash references a path outside this transaction")
            data = self.before[path][0]
            actual = hashlib.sha256(data).hexdigest() if data is not None else None
            if actual != digest:
                raise PatchError(f"base hash mismatch: {path}", "stale_preimage")

    def commit(self, changes):
        if changes.keys() != self.targets.keys():
            raise PatchError("commit paths differ from prepared transaction")
        with _COMMIT_LOCK:
            for path, target in self.targets.items():
                current = target.read_bytes() if target.exists() else None
                if current != self.before[path][0]:
                    raise PatchError(
                        f"preimage changed during preparation: {path}", "stale_preimage"
                    )
            written, directories = [], []
            try:
                for path, content in changes.items():
                    target = self.targets[path]
                    parent = target.parent
                    missing = []
                    while not parent.exists():
                        missing.append(parent)
                        parent = parent.parent
                    directories.extend(reversed(missing))
                    # Include the failing write in rollback in case it committed before raising.
                    written.append(path)
                    if content is None:
                        target.unlink()
                    else:
                        atomic_write_bytes(
                            target,
                            content.encode("utf-8"),
                            create_only=self.before[path][0] is None,
                        )
            except BaseException:
                failures = []
                for path in reversed(written):
                    target = self.targets[path]
                    data, mode = self.before[path]
                    try:
                        if data is None:
                            target.unlink(missing_ok=True)
                        else:
                            atomic_write_bytes(target, data)
                            target.chmod(mode)
                    except OSError as exc:
                        failures.append(str(exc))
                for directory in reversed(directories):
                    try:
                        directory.rmdir()
                    except OSError:
                        pass
                if failures:
                    raise RuntimeError(
                        "patch rollback unconfirmed: " + "; ".join(failures)
                    ) from None
                raise
