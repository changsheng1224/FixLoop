"""原子文件写入：先写临时文件再 replace，避免半截文件。"""

import os
import stat
import tempfile
from pathlib import Path


def atomic_write_text(
    path: Path, content: str, *, encoding: str = "utf-8", create_only: bool = False
) -> None:
    """将 content 原子写入 path（同目录 tmp → replace）。

    目标文件要么保持原样，要么被完整新内容替换；不会留下半截目标文件。
    失败时清理临时文件并重新抛出 OSError。
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    mode = stat.S_IMODE(path.stat().st_mode) if path.exists() else None
    fd, name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    tmp = Path(name)
    try:
        with os.fdopen(fd, "w", encoding=encoding) as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        if mode is not None:
            tmp.chmod(mode)
        if create_only:
            os.link(tmp, path)
        else:
            tmp.replace(path)
    finally:
        tmp.unlink(missing_ok=True)
