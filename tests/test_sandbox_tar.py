"""sandbox_tar 预检打包单测。"""

import io
import tarfile

import pytest

from src.harness.sandbox_tar import (
    SandboxArchiveError,
    build_sandbox_tar,
    should_skip_dir_name,
)


def _tar_member_names(tar_stream: io.BytesIO) -> set[str]:
    tar_stream.seek(0)
    with tarfile.open(fileobj=tar_stream, mode="r:") as tar:
        return {m.name for m in tar.getmembers()}


class TestShouldSkipDirName:
    def test_bonus_required_dirs_excluded(self):
        for name in (".git", ".venv", "venv", "node_modules"):
            assert should_skip_dir_name(name)

    def test_egg_info_suffix_excluded(self):
        assert should_skip_dir_name("my_pkg.egg-info")


class TestBuildSandboxTar:
    def test_excludes_heavy_dirs(self, tmp_path):
        (tmp_path / "main.py").write_text("x = 1\n", encoding="utf-8")
        (tmp_path / ".git").mkdir()
        (tmp_path / ".git" / "HEAD").write_text("ref\n", encoding="utf-8")
        (tmp_path / "node_modules").mkdir()
        (tmp_path / "node_modules" / "pkg.js").write_text("js", encoding="utf-8")
        (tmp_path / ".venv").mkdir()
        (tmp_path / ".venv" / "lib.py").write_text("lib", encoding="utf-8")

        tar_stream, stats = build_sandbox_tar(tmp_path)
        names = _tar_member_names(tar_stream)

        assert stats.file_count == 1
        assert "code/main.py" in names
        assert not any(".git" in n for n in names)
        assert not any("node_modules" in n for n in names)
        assert not any(".venv" in n for n in names)

    def test_size_limit_exceeded(self, tmp_path):
        (tmp_path / "big.bin").write_bytes(b"x" * 200)
        with pytest.raises(SandboxArchiveError) as exc_info:
            build_sandbox_tar(tmp_path, max_bytes=100)
        assert exc_info.value.code == "tar_size_exceeded"
        assert exc_info.value.total_bytes > 100

    def test_empty_after_exclude_raises(self, tmp_path):
        (tmp_path / ".git").mkdir()
        (tmp_path / ".git" / "HEAD").write_text("ref\n", encoding="utf-8")
        with pytest.raises(SandboxArchiveError) as exc_info:
            build_sandbox_tar(tmp_path)
        assert exc_info.value.code == "tar_empty"

    def test_normal_repo_stats(self, tmp_path):
        (tmp_path / "a.py").write_text("print(1)\n", encoding="utf-8")
        (tmp_path / "b.py").write_text("print(2)\n", encoding="utf-8")
        _, stats = build_sandbox_tar(tmp_path)
        assert stats.file_count == 2
        assert stats.total_bytes > 0
        assert stats.max_bytes == 200 * 1024 * 1024
