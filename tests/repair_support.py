"""Small repository builder shared by runtime and repair tests."""

import subprocess
from collections.abc import Mapping
from pathlib import Path


def build_repository(root: Path, files: Mapping[str, str], *, git: bool = False) -> Path:
    """Write UTF-8 fixtures in pytest-owned directories, optionally commit them."""
    root = Path(root).resolve()
    root.mkdir(parents=True, exist_ok=True)
    for name, content in files.items():
        path = (root / name).resolve()
        if not path.is_relative_to(root):
            raise ValueError(f"fixture path escapes repository: {name}")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
    if git:
        for args in (
            ["init"],
            ["config", "user.email", "test@test.com"],
            ["config", "user.name", "Test"],
            ["add", "."],
            ["-c", "commit.gpgsign=false", "commit", "-m", "Initial commit"],
        ):
            subprocess.run(
                ["git", *args],
                cwd=root,
                capture_output=True,
                text=True,
                check=True,
            )
    return root
