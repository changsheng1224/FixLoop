"""Shared repair language scope and project-local executable resolution."""

import os
import shutil
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class LanguageCapability:
    language: str
    extensions: tuple[str, ...]
    tests: bool = True


LANGUAGES = (
    LanguageCapability("python", (".py",)),
    LanguageCapability("javascript", (".js", ".jsx", ".mjs", ".cjs")),
    LanguageCapability("typescript", (".ts", ".tsx")),
    LanguageCapability("java", (".java",)),
    LanguageCapability("go", (".go",)),
    LanguageCapability("rust", (".rs",)),
    LanguageCapability("ruby", (".rb",), False),
    LanguageCapability("php", (".php",), False),
)


def editable_source(path: str) -> bool:
    return any(Path(path).suffix.lower() in item.extensions for item in LANGUAGES)


def source_files(root: Path, extensions: tuple[str, ...]) -> list[Path]:
    ignored = {
        ".git",
        ".pytest_cache",
        "__pycache__",
        "node_modules",
        "target",
        "dist",
        "build",
        ".venv",
        "venv",
        ".agent",
        ".runtime-tools",
    }
    files = []
    for directory, folders, names in os.walk(root, followlinks=False):
        folders[:] = [
            name
            for name in folders
            if name not in ignored and not (Path(directory) / name).is_symlink()
        ]
        files.extend(
            Path(directory) / name for name in names if Path(name).suffix.lower() in extensions
        )
    return sorted(files)


def project_command(root: Path, command: list[str]) -> list[str] | None:
    """Prefer local environments and checked-in build wrappers; never download tools."""
    tool, *args = command
    windows = os.name == "nt"
    candidates = []
    if tool == "pytest":
        from agent_runtime.test_execution import project_python

        return [project_python(root), "-m", "pytest", *args]
    if tool in {"tsc", "eslint", "jest", "vitest"}:
        candidates.append(root / "node_modules" / ".bin" / (tool + ".cmd" if windows else tool))
    if tool in {"mvn", "gradle"}:
        name = "mvnw" if tool == "mvn" else "gradlew"
        candidates.append(
            root
            / (name + ".cmd" if windows and tool == "mvn" else name + ".bat" if windows else name)
        )
    candidates.extend(
        root / name / (f"Scripts/{tool}.exe" if windows else f"bin/{tool}")
        for name in (".venv", "venv")
    )
    executable = next((str(p.resolve()) for p in candidates if p.is_file()), None)
    executable = executable or shutil.which(tool)
    if executable is None:
        return None
    if windows and Path(executable).suffix.lower() in {".cmd", ".bat"}:
        import subprocess

        return [
            os.environ.get("COMSPEC", "cmd.exe"),
            "/d",
            "/s",
            "/c",
            subprocess.list2cmdline([executable, *args]),
        ]
    return [executable, *args]
