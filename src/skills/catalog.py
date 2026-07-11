"""Load and cache Skill YAML definitions."""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from src.skills.models import SkillSpec
from src.skills.validate import format_report, validate_directory

_BUILTIN_DIR = Path(__file__).resolve().parent


class SkillCatalogError(ValueError):
    """Raised when Skill YAML cannot be loaded or validated."""


class SkillCatalog:
    """In-memory Skill registry."""

    def __init__(self, skills: list[SkillSpec]) -> None:
        self.skills = tuple(skills)

    @classmethod
    def load_from_directory(cls, directory: Path, *, strict: bool = True) -> "SkillCatalog":
        report = validate_directory(directory)
        if strict and not report.ok:
            raise SkillCatalogError(format_report(report, directory=directory))
        return cls(list(report.specs))


@lru_cache(maxsize=1)
def get_default_catalog() -> SkillCatalog:
    """Load built-in skills from ``src/skills/*.yaml``."""
    return SkillCatalog.load_from_directory(_BUILTIN_DIR)
