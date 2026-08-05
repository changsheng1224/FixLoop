"""Sandbox deps：声明依赖采集 + pip exit 保留 + PYTHONPATH。"""

from __future__ import annotations

from pathlib import Path

from src.harness.sandbox_manager import (
    sandbox_pip_install_command,
    sandbox_pythonpath_prefix,
)
from src.harness.sandbox_verify import (
    collect_declared_pip_packages,
    repo_needs_pip_install,
)


def test_pip_command_preserves_exit_and_pythonpath():
    cmd = sandbox_pip_install_command(extra_packages=["numpy", "evil;rm"], repo_path=None)
    assert "exit $ec" in cmd
    assert "| tail -20" not in cmd
    assert "pip install --user numpy" in cmd
    assert "evil" not in cmd  # 注入过滤
    assert "PYTHONPATH" in cmd


def test_pythonpath_includes_lib_when_present(tmp_path: Path):
    (tmp_path / "lib").mkdir()
    prefix = sandbox_pythonpath_prefix(tmp_path)
    assert "/code/lib" in prefix
    assert "/code" in prefix


def test_collect_declared_from_requirements(tmp_path: Path):
    (tmp_path / "requirements.txt").write_text(
        "numpy>=1.20\n# comment\nmpmath\n",
        encoding="utf-8",
    )
    pkgs = collect_declared_pip_packages(tmp_path)
    assert "numpy" in pkgs
    assert "mpmath" in pkgs
    assert repo_needs_pip_install(tmp_path)


def test_collect_from_setup_py_install_requires(tmp_path: Path):
    (tmp_path / "setup.py").write_text(
        "from setuptools import setup\n"
        "setup(install_requires=['packaging>=20', 'six'])\n",
        encoding="utf-8",
    )
    pkgs = collect_declared_pip_packages(tmp_path)
    assert "packaging" in pkgs
    assert "six" in pkgs
