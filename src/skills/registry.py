"""可执行 Skill Registry。"""

from __future__ import annotations

from collections.abc import Iterable
from pathlib import Path

import yaml

from src.skills.executable_spec import ExecutableSkillSpec

_DEFAULT_SPECS = Path(__file__).with_name("executable") / "specs.yaml"

ROUTER_VERSION = "1"


class SkillRegistryError(ValueError):
    """Registry 加载 / 查询错误。"""


class SkillRegistry:
    """进程内可执行 Skill 注册表（按 name 唯一，保留 version）。"""

    def __init__(self, specs: Iterable[ExecutableSkillSpec] | None = None) -> None:
        self._by_name: dict[str, ExecutableSkillSpec] = {}
        if specs:
            for spec in specs:
                self.register(spec)

    def register(self, spec: ExecutableSkillSpec, *, replace: bool = False) -> None:
        if spec.name in self._by_name and not replace:
            raise SkillRegistryError(f"skill already registered: {spec.name}")
        self._by_name[spec.name] = spec

    def get(self, name: str) -> ExecutableSkillSpec | None:
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
