"""Process lifetime ownership of a workbench state directory (not project files)."""

import os
from pathlib import Path

from src.workbench.models import ConflictError


class ServiceOwnership:
    """OS advisory lock; a crash releases ownership without deleting a lock file."""

    def __init__(self, path: Path):
        self._file = path.open("a+b")
        try:
            if not path.stat().st_size:
                self._file.write(b"\0")
                self._file.flush()
            self._file.seek(0)
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(self._file.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(self._file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            self._file.close()
            raise ConflictError("another workbench service owns this state directory") from exc

    def close(self):
        if self._file.closed:
            return
        try:
            self._file.seek(0)
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(self._file.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(self._file.fileno(), fcntl.LOCK_UN)
        finally:
            self._file.close()
