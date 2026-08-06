"""Memory Candidate schema 单测：规则/LLM 双路 + 冲突门控。"""

import pytest

from agent_runtime.features.memory.candidate import (
    Candidate,
    candidates_from_answer,
    candidates_from_tool,
    extract_from_final_answer,
    extract_from_stack,
    extract_from_tool_result,
    gate_candidate,
    llm_fill_candidate,
    promote_candidates,
)
from agent_runtime.features.memory.durable import DurableMemoryStore
from agent_runtime.providers.clients import FakeModelClient

# ── Candidate 构造与校验 ──


class TestCandidateSchema:
    def test_valid_candidate_constructs(self):
        c = Candidate(
            topic="key-decisions",
            key="fix-type-error",
            value="修复 calculator.py 的 TypeError",
            kind="decision",
            confidence=0.9,
            source="patcher",
        )
        assert c.topic == "key-decisions"
        assert c.key == "fix-type-error"

    def test_promotion_format(self):
        c = Candidate(
            topic="project-conventions",
            key="use-pytest",
            value="项目使用 pytest 进行测试",
            kind="fact",
            confidence=0.7,
            source="tool:read_file",
        )
        topic, text = c.promotion
        assert topic == "project-conventions"
        assert "use-pytest" in text
        assert "kind=fact" in text
        assert "pytest" in text

    def test_illegal_topic_raises(self):
        with pytest.raises(ValueError, match="非法 topic"):
            Candidate(
                topic="nonexistent-topic",
                key="k",
                value="v",
            )

    def test_illegal_kind_raises(self):
        with pytest.raises(ValueError, match="非法 kind"):
            Candidate(
                topic="key-decisions",
                key="k",
                value="v",
                kind="unknown",
            )

    def test_confidence_out_of_range_raises(self):
        with pytest.raises(ValueError, match="confidence"):
            Candidate(
                topic="key-decisions",
                key="k",
                value="v",
                confidence=1.5,
            )

    def test_empty_key_raises(self):
        with pytest.raises(ValueError, match="key 不能为空"):
            Candidate(topic="key-decisions", key="  ", value="v")

    def test_empty_value_raises(self):
        with pytest.raises(ValueError, match="value 不能为空"):
            Candidate(topic="key-decisions", key="k", value="")


# ── 规则抽取 ──


class TestExtractFromStack:
    def test_extracts_error_and_file(self):
        tb = (
            "Traceback (most recent call last):\n"
            '  File "src/calc.py", line 42, in add\n'
            "    return a + b\n"
            "TypeError: unsupported operand type(s) for +"
        )
        candidates = extract_from_stack(tb)
        assert len(candidates) >= 1
        errors = [c for c in candidates if c.kind == "error"]
        assert len(errors) >= 1
        assert errors[0].topic == "key-decisions"
        assert "calc.py" in errors[0].value

    def test_empty_returns_empty(self):
        assert extract_from_stack("") == []

    def test_multiple_files_extracted(self):
        tb = (
            "Traceback (most recent call last):\n"
            '  File "a.py", line 10, in foo\n'
            '  File "b.py", line 20, in bar\n'
            '  File "c.py", line 30, in baz\n'
            "TypeError: boom"
        )
        candidates = extract_from_stack(tb)
        errors = [c for c in candidates if c.kind == "error"]
        assert len(errors) == 3  # 前 3 个文件


class TestExtractFromToolResult:
    def test_error_detection_creates_candidate(self):
        candidates = extract_from_tool_result(
            "run_shell",
            {"command": "pytest"},
            "Error: assert 3 == 5\nFAILED test_add\n",
        )
        assert len(candidates) >= 1
        assert candidates[0].kind == "error"
        assert candidates[0].topic == "key-decisions"

    def test_git_blame_creates_dependency_fact(self):
        candidates = extract_from_tool_result(
            "git_blame",
            {"path": "src/calc.py"},
            "abc123 (John 2024-01-01) def add(a,b): return a+b",
        )
        assert len(candidates) >= 1
        deps = [c for c in candidates if c.topic == "dependency-facts"]
        assert len(deps) >= 1

    def test_empty_result_returns_empty(self):
        assert extract_from_tool_result("read_file", {}, "") == []


class TestExtractFromFinalAnswer:
    def test_decision_keywords_extracted(self):
        candidates = extract_from_final_answer(
            "根因是 calculator.py:42 的类型转换错误。修复方式：添加 int() 转换。",
        )
        decisions = [c for c in candidates if c.kind == "decision"]
        assert len(decisions) >= 1
        assert decisions[0].topic == "key-decisions"

    def test_convention_keywords_extracted(self):
        candidates = extract_from_final_answer(
            "项目应该总是使用 pytest 进行测试，必须遵守 ruff 格式规范。",
        )
        conventions = [c for c in candidates if c.topic == "project-conventions"]
        assert len(conventions) >= 1


# ── Hook 入口 ──


class TestHookCandidates:
    def test_candidates_from_tool(self):
        candidates = candidates_from_tool(
            "run_shell",
            {"command": "pytest"},
            "FAILED: test_add - AssertionError",
        )
        assert len(candidates) >= 1

    def test_candidates_from_answer(self):
        candidates = candidates_from_answer(
            "修复了 import 错误，应该添加 __init__.py。",
            issue="ImportError at app.py:5",
        )
        assert len(candidates) >= 1


# ── LLM fill ──


class TestLLMFillCandidate:
    def test_llm_fills_kind_and_confidence(self):
        client = FakeModelClient(['{"kind": "error", "confidence": 0.95}'])
        c = Candidate(
            topic="key-decisions",
            key="test-key",
            value="TypeError at calc.py:42",
            kind="observation",
            confidence=0.5,
            source="tool",
        )
        result = llm_fill_candidate(c, client)
        assert result.kind == "error"
        assert result.confidence == 0.95

    def test_llm_fallback_on_bad_output(self):
        client = FakeModelClient(["not valid json at all"])
        c = Candidate(
            topic="key-decisions",
            key="test-key",
            value="test value",
        )
        result = llm_fill_candidate(c, client)
        # 保持原值
        assert result.kind == "observation"
        assert result.confidence == 0.5

    def test_llm_does_not_change_topic(self):
        client = FakeModelClient(['{"kind": "fact", "confidence": 0.8, "topic": "hacked"}'])
        c = Candidate(
            topic="project-conventions",
            key="k",
            value="v",
        )
        result = llm_fill_candidate(c, client)
        # topic 不变（LLM 只填 kind/confidence）
        assert result.topic == "project-conventions"


# ── 冲突门控 ──


class TestGateCandidate:
    @pytest.fixture
    def store(self, tmp_path):
        (tmp_path / ".agent" / "memory").mkdir(parents=True)
        return DurableMemoryStore(str(tmp_path))

    def test_new_candidate_allowed(self, store):
        c = Candidate(
            topic="project-conventions",
            key="use-pytest",
            value="项目使用 pytest",
        )
        gate = gate_candidate(c, [])
        assert gate.allowed is True
        assert gate.resolution == "new"

    def test_illegal_topic_rejected(self):
        c = Candidate(
            topic="key-decisions",
            key="k",
            value="v",
        )
        c.topic = "hacked-topic"  # 绕过构造检查
        gate = gate_candidate(c, [])
        assert gate.allowed is False
        assert "非法 topic" in gate.reason

    def test_duplicate_rejected(self, store):
        c = Candidate(
            topic="project-conventions",
            key="use-pytest",
            value="项目使用 pytest",
        )
        _, text = c.promotion
        # 写入一条
        store.promote([c.promotion])
        # 同 key 再次门控
        topic_file = store.topics_dir / "project-conventions.md"
        existing = store._read_topic(topic_file)
        gate = gate_candidate(c, existing)
        assert gate.allowed is False

    def test_conflict_state_machine(self, store):
        """同 key 冲突走 _resolve_conflict 状态机。"""
        c1 = Candidate(
            topic="key-decisions",
            key="decision-1",
            value="原始决策",
            confidence=0.6,
            source="patcher",
        )
        store.promote([c1.promotion])

        # 同 key 但内容不同 → 应判定为冲突
        c2 = Candidate(
            topic="key-decisions",
            key="decision-1",
            value="修改后的决策 [authority:agent]",
            confidence=0.9,
            source="user",
        )
        topic_file = store.topics_dir / "key-decisions.md"
        existing = store._read_topic(topic_file)
        gate = gate_candidate(c2, existing, authority="agent")
        # agent > auto → override
        assert gate.allowed is True
        assert gate.resolution == "override"


# ── promote_candidates 集成 ──


class TestPromoteCandidates:
    @pytest.fixture
    def store(self, tmp_path):
        (tmp_path / ".agent" / "memory").mkdir(parents=True)
        return DurableMemoryStore(str(tmp_path))

    def test_illegal_topic_not_promoted(self, store):
        candidates = [
            Candidate(
                topic="key-decisions",
                key="ok",
                value="valid",
            ),
        ]
        # 手动破坏 topic
        candidates[0].topic = "bad-topic"
        written = promote_candidates(store, candidates)
        assert written == 0
