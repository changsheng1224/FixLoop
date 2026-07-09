"""原子文件写入：先写临时文件再 replace，避免半截文件。"""

from pathlib import Path


def atomic_write_text(path: Path, content: str, *, encoding: str = "utf-8") -> None:
    """将 content 原子写入 path（同目录 tmp → replace）。

    目标文件要么保持原样，要么被完整新内容替换；不会留下半截目标文件。
    失败时清理临时文件并重新抛出 OSError。
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    try:
        tmp.write_text(content, encoding=encoding)
        tmp.replace(path)
    except OSError:
        tmp.unlink(missing_ok=True)
        raise
