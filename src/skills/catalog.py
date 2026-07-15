"""Load and cache Skill YAML definitions."""

from __future__ import annotations

import hashlib
import json
from functools import lru_cache
from pathlib import Path
from typing import TYPE_CHECKING

from src.skills.models import SkillSpec
from src.skills.validate import format_report, validate_directory

if TYPE_CHECKING:
    from agent_runtime.features.memory.semantic import SemanticMemory

_BUILTIN_DIR = Path(__file__).resolve().parent


class SkillCatalogError(ValueError):
    """Raised when Skill YAML cannot be loaded or validated."""


class SkillCatalog:
    """In-memory Skill registry（含向量索引用于 N>100 大目录场景）。"""

    def __init__(self, skills: list[SkillSpec], content_hash: str = "") -> None:
        self.skills = tuple(skills)
        self.content_hash = content_hash or _compute_skills_hash(
            Path.cwd() / ".agent" / ".skill_cache"  # default path, overridden by caller
        )
        self._embed_index: SemanticMemory | None = None

    def build_embed_index(self) -> bool:
        """预构建语义向量索引（复用 semantic.py SemanticMemory）。

        N>50 时调用，后续 match_skill_semantic 直接使用索引。
        返回 True 表示索引构建成功，False 表示模型不可用。
        """
        try:
            from agent_runtime.features.memory.semantic import SemanticMemory

            sem = SemanticMemory()
            if not sem.available:
                self._embed_index = None
                return False
            for spec in self.skills:
                example = getattr(spec, "example_issue", "")[:200]
                sem.add(
                    {
                        "text": f"{spec.name}: {spec.trigger_pattern} {example}",
                    }
                )
            self._embed_index = sem
            return True
        except Exception:
            self._embed_index = None
            return False

    def get_embed_index(self):
        """获取预构建的向量索引（None 表示不可用或未构建）。"""
        if self._embed_index is not None:
            return self._embed_index
        self.build_embed_index()
        return self._embed_index

    @classmethod
    def load_from_directory(cls, directory: Path, *, strict: bool = True) -> SkillCatalog:
        report = validate_directory(directory)
        if strict and not report.ok:
            raise SkillCatalogError(format_report(report, directory=directory))
        content_hash = _compute_directory_hash(directory)
        return cls(list(report.specs), content_hash=content_hash)

    @property
    def skill_count(self) -> int:
        return len(self.skills)

    def rebuild_index(self, cache_path: Path) -> bool:
        """原子 rebuild skill 索引。

        写 temp → rename，避免读写竞争。返回 True 表示索引已更新。

        Args:
            cache_path: 索引文件路径（如 .agent/.skill_index.json）。

        Returns:
            True 表示索引已重建，False 表示内容未变化无需重建。
        """
        cache_path.parent.mkdir(parents=True, exist_ok=True)

        # 检查是否需要重建
        if cache_path.is_file():
            try:
                existing = json.loads(cache_path.read_text(encoding="utf-8"))
                if existing.get("content_hash") == self.content_hash:
                    return False  # 未变化
            except (json.JSONDecodeError, OSError):
                pass

        # 原子写入：temp → rename
        tmp = cache_path.with_suffix(".tmp")
        payload = {
            "content_hash": self.content_hash,
            "skill_count": self.skill_count,
            "skills": [
                {
                    "name": s.name,
                    "language": s.language,
                    "priority": s.priority,
                    "trigger_pattern": s.trigger_pattern,
                }
                for s in self.skills
            ],
        }
        tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(cache_path)
        return True


def _compute_directory_hash(directory: Path) -> str:
    """计算目录下所有 YAML 文件的 SHA256。"""
    h = hashlib.sha256()
    for yaml_file in sorted(directory.glob("*.yaml")):
        try:
            h.update(yaml_file.read_bytes())
        except OSError:
            pass
    return h.hexdigest()


def _compute_skills_hash(fallback_path: Path | None = None) -> str:
    """兼容旧构造的 hash 回退。"""
    return ""


@lru_cache(maxsize=1)
def get_default_catalog() -> SkillCatalog:
    """Load built-in skills from ``src/skills/*.yaml``."""
    return SkillCatalog.load_from_directory(_BUILTIN_DIR)
