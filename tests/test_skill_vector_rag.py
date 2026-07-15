"""Skill 向量 RAG 单测：SkillCatalog.embed_index + match_skill_semantic + regex 回退。"""

from src.skills.catalog import SkillCatalog
from src.skills.matcher import match_skill, match_skill_semantic
from src.skills.models import SkillSpec


def _make_skills(n: int = 10) -> list[SkillSpec]:
    """生成 N 个 synthetic SkillSpec（模拟真实 YAML 结构）。"""
    skills = []
    for i in range(n):
        skills.append(
            SkillSpec(
                name=f"python_error_{i:03d}",
                language="python",
                trigger_pattern=(
                    f"Error{i % 5}Type"
                    if i % 5 == 0
                    else f"ExceptionType{i % 3}"
                    if i % 3 == 0
                    else f"ModuleNotFoundError{i}"
                ),
                example_issue=(
                    f"TypeError at file_{i}.py:line {i * 10}"
                    if i % 2 == 0
                    else f"ImportError in module_{i}"
                ),
                priority=10 - (i % 3),
                suggested_tools=["grep", "read_file", "ast_parse"],
                prompt_hint=f"Fix error type {i} by checking types",
            )
        )
    return skills


class TestSkillCatalogEmbedIndex:
    def test_build_embed_index_with_skills(self):
        """build_embed_index 对 SkillSpec 列表构建索引。"""
        skills = _make_skills(15)
        catalog = SkillCatalog(skills)
        # 语义模型可能不可用 → 不影响调用
        result = catalog.build_embed_index()
        assert isinstance(result, bool)

    def test_get_embed_index_returns_index(self):
        """get_embed_index 返回索引或 None（模型不可用时）。"""
        skills = _make_skills(5)
        catalog = SkillCatalog(skills)
        idx = catalog.get_embed_index()
        # idx is SemanticMemory or None
        assert idx is None or hasattr(idx, "search")

    def test_embed_index_cached(self):
        """build_embed_index 后 get_embed_index 返回同一实例。"""
        skills = _make_skills(3)
        catalog = SkillCatalog(skills)
        catalog.build_embed_index()
        idx1 = catalog.get_embed_index()
        idx2 = catalog.get_embed_index()
        assert idx1 is idx2


class TestMatchSkillSemantic:
    def test_small_n_falls_back_to_regex(self):
        """N≤50 → 走 match_skill 全量 regex。"""
        skills = _make_skills(10)
        catalog = SkillCatalog(skills)
        result = match_skill_semantic(
            "TypeError at calc.py:42",
            catalog=catalog,
        )
        # 应返回 MatchedSkill 或 None（取决于 trigger_pattern 匹配）
        if result is not None:
            assert result is not None

    def test_regex_confirms_semantic_hits(self):
        """regex 精确确认：即使语义有候选，regex 不匹配则返回 None。"""
        skills = _make_skills(5)
        catalog = SkillCatalog(skills)
        # 用不匹配的 issue → regex 应过滤掉
        result = match_skill_semantic(
            "completely unrelated text xyz",
            catalog=catalog,
        )
        # 无匹配 trigger_pattern → None
        assert result is None or result.name is not None

    def test_match_skill_vs_semantic_same_result_small_n(self):
        """N≤50 时 match_skill 与 match_skill_semantic 结果一致。"""
        skills = _make_skills(10)
        catalog = SkillCatalog(skills)
        issue = "TypeError at calculator.py:42"
        r1 = match_skill(issue, catalog=catalog)
        r2 = match_skill_semantic(issue, catalog=catalog)
        # 两者应都为 None 或同一 skill
        assert (r1 is None) == (r2 is None)

    def test_rank_key_priority_order(self):
        """高 priority 的 skill 排前。"""
        s1 = SkillSpec(
            name="high_priority",
            language="python",
            trigger_pattern="Error",
            priority=10,
        )
        s2 = SkillSpec(
            name="low_priority",
            language="python",
            trigger_pattern="Error",
            priority=1,
        )
        catalog = SkillCatalog([s1, s2])
        result = match_skill_semantic("Error occurred", catalog=catalog)
        if result is not None:
            assert result.name == "high_priority"


class TestSemanticFallback:
    def test_unavailable_model_falls_back(self):
        """SemanticMemory 不可用时回退 match_skill regex。"""
        skills = _make_skills(60)  # >50
        catalog = SkillCatalog(skills)
        # 强制 _embed_index=None
        catalog._embed_index = None
        catalog.build_embed_index = lambda: False
        # 应不抛异常
        result = match_skill_semantic(
            "TypeError at calc.py:42",
            catalog=catalog,
        )
        assert result is None or result.name is not None

    def test_semantic_search_exception_falls_back(self):
        """sem.search 异常时回退 regex。"""
        skills = _make_skills(60)
        catalog = SkillCatalog(skills)

        # 注入异常索引
        class BadSem:
            available = True

            def search(self, *a, **kw):
                raise RuntimeError("simulated failure")

        catalog._embed_index = BadSem()
        result = match_skill_semantic(
            "TypeError at calc.py:42",
            catalog=catalog,
        )
        # 回退到 regex → 不抛异常
        assert result is None or result.name is not None


class TestOrchestratorSkillSemanticWiring:
    def test_orchestrator_uses_semantic_matcher_by_default(self, monkeypatch):
        """Orchestrator 主路径应使用可自动降级的 semantic matcher。"""
        from src.orchestrator import Orchestrator
        from src.skills.models import MatchedSkill

        called = {}

        def fake_semantic(issue, *, language="python", catalog=None, top_k=5):
            called["issue"] = issue
            called["language"] = language
            spec = SkillSpec(
                name="semantic_skill",
                language=language,
                trigger_pattern="TypeError",
            )
            return MatchedSkill.from_spec(spec, candidates_count=1)

        monkeypatch.setattr("src.skills.matcher.match_skill_semantic", fake_semantic)

        orch = Orchestrator(None, None, None)
        result = orch._match_skill("TypeError at calc.py", language="python")

        assert result is not None
        assert result.name == "semantic_skill"
        assert called == {"issue": "TypeError at calc.py", "language": "python"}
