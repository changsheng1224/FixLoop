"""可执行 Skill Registry。"""

from __future__ import annotations

from collections.abc import Iterable
from pathlib import Path

import yaml

from src.skills.contract import (
    CanonicalSkillSpec,
    canonical_from_executable,
    canonical_from_guidance,
)
from src.skills.executable_spec import ExecutableSkillSpec

_DEFAULT_SPECS = Path(__file__).with_name("executable") / "specs.yaml"

ROUTER_VERSION = "1"


class SkillRegistryError(ValueError):
    """Registry 加载 / 查询错误。"""


class CanonicalSkillRegistry:
    """Unified read model over guidance and executable Skill identities."""

    def __init__(self) -> None:
        self._items: dict[tuple[str, str, str], CanonicalSkillSpec] = {}

    def register(self, spec: CanonicalSkillSpec) -> None:
        governed = spec.with_hash()
        key = (governed.name, governed.version, governed.kind.value)
        existing = self._items.get(key)
        if existing is not None and existing.content_hash != governed.content_hash:
            raise SkillRegistryError(
                f"canonical identity conflict: {governed.name}@{governed.version}:{governed.kind}"
            )
        self._items[key] = governed

    def list(self, *, name: str = "", lifecycle: str | None = None) -> list[CanonicalSkillSpec]:
        items = list(self._items.values())
        if name:
            items = [item for item in items if item.name == name]
        if lifecycle is not None:
            items = [item for item in items if item.lifecycle.value == lifecycle]
        return sorted(items, key=lambda item: (item.name, item.version, item.kind.value))

    def resolve(
        self, name: str, *, version: str = "", kind: str = ""
    ) -> CanonicalSkillSpec | None:
        items = [item for item in self.list(name=name) if not kind or item.kind.value == kind]
        if version:
            items = [item for item in items if item.version == version]
        if not items:
            return None
        return sorted(
            items,
            key=lambda item: tuple(int(x) for x in item.version.split("-")[0].split(".")),
            reverse=True,
        )[0]

    @classmethod
    def from_legacy(cls, *, executable_registry=None, guidance_catalog=None):
        registry = cls()
        if executable_registry is not None:
            for spec in executable_registry.list(lifecycle=None):
                registry.register(canonical_from_executable(spec))
        if guidance_catalog is not None:
            for spec in guidance_catalog.skills:
                registry.register(canonical_from_guidance(spec))
        return registry


class SkillRegistry:
    """进程内可执行 Skill 注册表（按 name 唯一，保留 version）。"""

    def __init__(self, specs: Iterable[ExecutableSkillSpec] | None = None) -> None:
        self._by_name: dict[str, ExecutableSkillSpec] = {}
        self._by_version: dict[tuple[str, str], ExecutableSkillSpec] = {}
        if specs:
            for spec in specs:
                self.register(spec)

    def register(self, spec: ExecutableSkillSpec, *, replace: bool = False) -> None:
        if spec.name in self._by_name and not replace:
            raise SkillRegistryError(f"skill already registered: {spec.name}")
        self._by_name[spec.name] = spec
        self._by_version[(spec.name, spec.version)] = spec

    def register_version(self, spec: ExecutableSkillSpec) -> None:
        """Register another immutable version without replacing the active pointer."""
        key = (spec.name, spec.version)
        if key in self._by_version:
            raise SkillRegistryError(
                f"skill version already registered: {spec.name}@{spec.version}"
            )
        self._by_version[key] = spec
        self._by_name.setdefault(spec.name, spec)

    def get(self, name: str, version: str | None = None) -> ExecutableSkillSpec | None:
        if version:
            return self._by_version.get((name, version))
        return self._by_name.get(name)

    def require(self, name: str) -> ExecutableSkillSpec:
        spec = self.get(name)
        if spec is None:
            raise SkillRegistryError(f"unknown skill: {name}")
        return spec

    def list(
        self,
        *,
        lifecycle: str | None = "active",
        names: Iterable[str] | None = None,
    ) -> list[ExecutableSkillSpec]:
        items = list(self._by_name.values())
        if names is not None:
            allow = set(names)
            items = [s for s in items if s.name in allow]
        if lifecycle is not None:
            items = [s for s in items if s.lifecycle == lifecycle]
        return sorted(items, key=lambda s: s.name)

    def resolve_version(self, name: str) -> dict[str, str]:
        spec = self.require(name)
        return {
            "name": spec.name,
            "version": spec.version,
            "lifecycle": spec.lifecycle,
            "fallback": spec.fallback,
        }

    def versions(self, name: str) -> list[ExecutableSkillSpec]:
        return sorted(
            [spec for (skill_name, _), spec in self._by_version.items() if skill_name == name],
            key=lambda spec: tuple(int(x) for x in spec.version.split("-")[0].split(".")),
            reverse=True,
        )

    def canonical(self, name: str, version: str | None = None):
        spec = self.get(name, version)
        return canonical_from_executable(spec) if spec is not None else None

    def verify_integrity(
        self, name: str, version: str | None = None, expected_hash: str = ""
    ) -> bool:
        canonical = self.canonical(name, version)
        if canonical is None:
            return False
        return not expected_hash or canonical.content_hash == expected_hash

    def load_yaml(self, path: Path | str, *, replace: bool = True) -> int:
        path = Path(path)
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        skills = data.get("skills") if isinstance(data, dict) else data
        if not isinstance(skills, list):
            raise SkillRegistryError(f"expected skills list in {path}")
        n = 0
        for raw in skills:
            spec = ExecutableSkillSpec.model_validate(raw)
            self.register(spec, replace=replace)
            n += 1
        return n

    @classmethod
    def from_default_specs(cls) -> SkillRegistry:
        reg = cls()
        reg.load_yaml(_DEFAULT_SPECS)
        return reg


_default_registry: SkillRegistry | None = None


def get_default_executable_registry() -> SkillRegistry:
    global _default_registry
    if _default_registry is None:
        _default_registry = SkillRegistry.from_default_specs()
    return _default_registry


def reset_default_executable_registry_for_tests() -> None:
    global _default_registry
    _default_registry = None
