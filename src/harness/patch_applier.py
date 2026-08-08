"""PatchApplier：原子化补丁应用与回滚。

原则：
- 逐个应用补丁，任一失败则全部回滚
- 文件级回滚（cp file.py.bak.timestamp → file.py）
- 限制：单轮最多 5 个补丁、单补丁最多 50 行
"""

import base64
import posixpath
import shlex

from src.state import CandidatePatch

MAX_PATCHES = 5
MAX_LINES = 50
BACKUP_RETENTION = 3


class PatchApplier:
    """在 Sandbox 内逐个应用 CandidatePatch，失败时文件级回滚。"""

    def __init__(self, sandbox_manager):
        self.manager = sandbox_manager

    def apply(self, sandbox, patches: list[CandidatePatch]) -> list[bool]:
        """逐个应用补丁，任一失败则全部回滚。

        Returns:
            [True, False, ...] 各补丁应用结果。
        """
        if len(patches) > MAX_PATCHES:
            return [False] * len(patches)

        results = []
        applied = []
        for i, patch in enumerate(patches[:MAX_PATCHES]):
            if len(patch.diff.splitlines()) > MAX_LINES:
                results.append(False)
                continue

            rel_path = _safe_rel_path(patch.file_path)
            if rel_path is None:
                results.append(False)
                self._revert_all(sandbox, applied)
                results.extend([False] * (len(patches) - len(results)))
                return results
            encoded = base64.b64encode(patch.diff.encode("utf-8")).decode("ascii")

            cmd = (
                f"printf '%s' {shlex.quote(encoded)} | base64 -d > "
                f"/tmp/patch_{i}.diff && /entrypoint.sh apply-patch "
                f"{shlex.quote('/code/' + rel_path)} /tmp/patch_{i}.diff"
            )
            result = self.manager.execute(sandbox, cmd, timeout=30)
            ok = result.exit_code == 0
            results.append(ok)
            if ok:
                applied.append(patch)
            else:
                # Revert the current patch too: an entrypoint may have
                # partially written before returning a non-zero status.
                self._revert_all(sandbox, applied + [patch])
                # 残余标记为失败
                results.extend([False] * (len(patches) - len(results)))
                return results

        return results

    def _revert_all(self, sandbox, patches: list[CandidatePatch]):
        for patch in reversed(patches):
            self.manager.execute(
                sandbox,
                f"/entrypoint.sh revert-patch /code/{patch.file_path}",
                timeout=10,
            )


def _escape(text: str) -> str:
    """简单 shell 转义。"""
    return text.replace("'", "'\\''")


def _safe_rel_path(path: str) -> str | None:
    """Return a normalized relative POSIX path safe for the sandbox command."""
    raw = str(path or "").replace("\\", "/")
    if not raw or raw.startswith("/"):
        return None
    normalized = posixpath.normpath(raw)
    if normalized in {".", ".."} or normalized.startswith("../"):
        return None
    return normalized
