"""One versioned registry for guidance and executable Skills."""
from __future__ import annotations

from collections.abc import Iterable
from functools import lru_cache
from pathlib import Path

import yaml

from src.skills.contract import SkillKind, SkillSpec

ROUTER_VERSION = "2"


class SkillRegistryError(ValueError):
    """Invalid or ambiguous Skill identity."""


class SkillRegistry:
    def __init__(self, specs: Iterable[SkillSpec] = ()) -> None:
        self._items: dict[tuple[str, str, SkillKind], SkillSpec] = {}
        self._active: dict[tuple[str, SkillKind], str] = {}
        for spec in specs:
            self.register(spec)

    def register(self, spec: SkillSpec, *, activate: bool = True) -> None:
        if not isinstance(spec, SkillSpec):
            raise TypeError("registry requires SkillSpec")
        governed = spec.with_hash()
        key = (governed.name, governed.version, governed.kind)
        existing = self._items.get(key)
        if existing is not None and existing.content_hash != governed.content_hash:
            raise SkillRegistryError(f"immutable skill identity conflict: {key}")
        self._items[key] = governed
        if activate:
            self._active[(governed.name, governed.kind)] = governed.version

    def get(
        self, name: str, version: str | None = None, *, kind: SkillKind | str | None = None
    ) -> SkillSpec | None:
        items = [
            spec for (item_name, item_version, item_kind), spec in self._items.items()
            if item_name == name and (kind is None or item_kind == kind)
            and (item_version == version if version else
                 self._active.get((item_name, item_kind)) == item_version)
        ]
        if len(items) > 1:
            raise SkillRegistryError(f"ambiguous skill {name!r}; specify kind")
        return items[0].model_copy(deep=True) if items else None

    def require(self, name: str, *, kind: SkillKind | str | None = None) -> SkillSpec:
        spec = self.get(name, kind=kind)
        if spec is None:
            raise SkillRegistryError(f"unknown skill: {name}")
        return spec

    def list(
        self, *, lifecycle: str | None = "active", names: Iterable[str] | None = None,
        name: str = "", kind: SkillKind | str | None = None, all_versions: bool = False,
    ) -> list[SkillSpec]:
        allowed = set(names) if names is not None else None
        return [
            spec.model_copy(deep=True)
            for key, spec in sorted(self._items.items())
            if (not name or spec.name == name)
            and (allowed is None or spec.name in allowed)
            and (kind is None or spec.kind == kind)
            and (lifecycle is None or spec.lifecycle == lifecycle)
            and (all_versions or self._active.get((key[0], key[2])) == key[1])
        ]

    def verify_integrity(self, name: str, version: str, expected_hash: str, *, kind=None) -> bool:
        spec = self.get(name, version, kind=kind)
        return spec is not None and spec.content_hash == expected_hash == spec.stable_hash()

    def load_yaml(self, path: Path | str) -> int:
        data = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
        records = data["skills"] if isinstance(data, dict) and "skills" in data else [data]
        specs = [SkillSpec.model_validate(raw) for raw in records]
        for spec in specs:
            self.register(spec)
        return len(specs)

    @classmethod
    def from_default_specs(cls) -> SkillRegistry:
        registry = cls()
        root = Path(__file__).resolve().parent
        for path in sorted(root.glob("*.yaml")):
            registry.load_yaml(path)
        registry.load_yaml(root / "executable" / "specs.yaml")
        return registry


@lru_cache(maxsize=1)
def get_default_registry() -> SkillRegistry:
    return SkillRegistry.from_default_specs()
