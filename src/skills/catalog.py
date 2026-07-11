"""Load and cache Skill YAML definitions."""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

import yaml
from pydantic import ValidationError

from src.skills.models import SkillSpec

_BUILTIN_DIR = Path(__file__).resolve().parent


class SkillCatalogError(ValueError):
    """Raised when Skill YAML cannot be loaded or validated."""


class SkillCatalog:
    """In-memory Skill registry."""

    def __init__(self, skills: list[SkillSpec]) -> None:
        self.skills = tuple(skills)

    @classmethod
    def load_from_directory(cls, directory: Path) -> "SkillCatalog":
        if not directory.is_dir():
            raise SkillCatalogError(f"skills directory not found: {directory}")

        loaded: list[SkillSpec] = []
        seen_names: set[str] = set()
        for yaml_file in sorted(directory.glob("*.yaml")):
            try:
                raw = yaml.safe_load(yaml_file.read_text(encoding="utf-8"))
            except Exception as exc:
                raise SkillCatalogError(f"failed to read {yaml_file}: {exc}") from exc
            if not isinstance(raw, dict):
                raise SkillCatalogError(f"skill file must be a mapping: {yaml_file}")
            try:
                spec = SkillSpec.model_validate(raw)
            except ValidationError as exc:
                raise SkillCatalogError(f"invalid skill schema in {yaml_file}: {exc}") from exc
            if spec.name in seen_names:
                raise SkillCatalogError(f"duplicate skill name {spec.name!r} in {directory}")
            seen_names.add(spec.name)
            loaded.append(spec)
        return cls(loaded)


@lru_cache(maxsize=1)
def get_default_catalog() -> SkillCatalog:
    """Load built-in skills from ``src/skills/*.yaml``."""
    return SkillCatalog.load_from_directory(_BUILTIN_DIR)
