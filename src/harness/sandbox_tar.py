"""Sandbox tar 打包：目录排除、大小上限、预检（先于容器创建）。"""

from __future__ import annotations

import io
import os
import tarfile
from dataclasses import dataclass
from pathlib import Path

DEFAULT_SANDBOX_TAR_MAX_MB = 200

SANDBOX_TAR_EXCLUDE_DIR_NAMES = frozenset(
    {
        ".git",
        ".venv",
        "venv",
        "node_modules",
        "__pycache__",
        ".pytest_cache",
        ".agent",
        ".ruff_cache",
        ".idea",
        ".vscode",
        "dist",
        "build",
        ".eggs",
    }
)

SANDBOX_TAR_SKIP_FILE_SUFFIXES = (".pyc", ".pyo")
SANDBOX_TAR_SKIP_DIR_SUFFIXES = (".egg-info",)


@dataclass(frozen=True)
class TarBuildStats:
    """打包统计（写入 Sandbox.timings / verify internal）。"""

    total_bytes: int
    file_count: int
    max_bytes: int
    tar_stream_size: int


class SandboxArchiveError(Exception):
    """tar 预检失败（超限或无可打包文件）。"""

    code: str

    def __init__(
        self,
        message: str,
        *,
        code: str,
        total_bytes: int = 0,
        max_bytes: int = 0,
        file_count: int = 0,
    ):
        super().__init__(message)
        self.code = code
        self.total_bytes = total_bytes
        self.max_bytes = max_bytes
        self.file_count = file_count


def sandbox_tar_max_bytes() -> int:
    """未压缩累计字节上限（默认 200MB）。"""
    raw = os.getenv("FIXLOOP_SANDBOX_TAR_MAX_MB", str(DEFAULT_SANDBOX_TAR_MAX_MB))
    try:
        mb = int(raw)
    except ValueError:
        mb = DEFAULT_SANDBOX_TAR_MAX_MB
    return max(mb, 1) * 1024 * 1024


def should_skip_dir_name(name: str) -> bool:
    if name in SANDBOX_TAR_EXCLUDE_DIR_NAMES:
        return True
    return any(name.endswith(suffix) for suffix in SANDBOX_TAR_SKIP_DIR_SUFFIXES)


def should_skip_file(path: Path) -> bool:
    if path.name.endswith(SANDBOX_TAR_SKIP_FILE_SUFFIXES):
        return True
    try:
        if path.is_symlink():
            return True
    except OSError:
        return True
    return False


def build_sandbox_tar(
    src: Path,
    dst: str = "/code",
    *,
    max_bytes: int | None = None,
) -> tuple[io.BytesIO, TarBuildStats]:
    """遍历 *src* 打包为 tar，超限时抛 ``SandboxArchiveError``。"""
    root = Path(src).resolve()
    if not root.is_dir():
        raise SandboxArchiveError(
            f"仓库路径不是目录: {src}",
            code="tar_invalid_repo",
        )

    limit = max_bytes if max_bytes is not None else sandbox_tar_max_bytes()
    total_bytes = 0
    file_count = 0
    tar_stream = io.BytesIO()

    with tarfile.open(fileobj=tar_stream, mode="w") as tar:
        for walk_root, dirs, files in os.walk(root):
            dirs[:] = [d for d in dirs if not should_skip_dir_name(d)]
            for name in files:
                full = Path(walk_root) / name
                if should_skip_file(full):
                    continue
                try:
                    size = full.stat().st_size
                except OSError:
                    continue
                next_total = total_bytes + size
                if next_total > limit:
                    raise SandboxArchiveError(
                        f"tar 打包超限: {next_total} 字节 > 上限 {limit} 字节 "
                        f"({limit // (1024 * 1024)} MB)",
                        code="tar_size_exceeded",
                        total_bytes=next_total,
                        max_bytes=limit,
                        file_count=file_count,
                    )
                arcname = str(Path(dst) / full.relative_to(root)).replace("\\", "/")
                tar.add(full, arcname=arcname)
                total_bytes = next_total
                file_count += 1

    if file_count == 0:
        raise SandboxArchiveError(
            f"tar 打包结果为空（排除后无可传文件）: {root}",
            code="tar_empty",
            max_bytes=limit,
        )

    tar_stream.seek(0)
    stats = TarBuildStats(
        total_bytes=total_bytes,
        file_count=file_count,
        max_bytes=limit,
        tar_stream_size=tar_stream.getbuffer().nbytes,
    )
    return tar_stream, stats
