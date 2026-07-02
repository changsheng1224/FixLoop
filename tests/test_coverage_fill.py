"""补充测试：semantic.py 和 context_manager.py 低覆盖路径。"""


from agent_runtime.config import AgentConfig
from agent_runtime.context_manager import (
    ContextManager,
    TokenBudget,
    _truncate_tool_content,
)
from agent_runtime.features.memory.semantic import SemanticMemory
from agent_runtime.providers.clients import FakeModelClient
from agent_runtime.runtime import Agent
from agent_runtime.workspace import WorkspaceContext


class TestSemanticMemoryEdge:
    """semantic.py 边界测试。"""

    def test_available_when_model_none(self):
        sem = SemanticMemory()
        assert isinstance(sem.available, bool)

    def test_add_empty_text_skipped(self):
        sem = SemanticMemory()
        sem.add({"text": ""})
        assert len(sem._notes) == 0

    def test_search_with_empty_notes(self):
        sem = SemanticMemory()
        results = sem.search("query")
        assert results == []

    def test_search_no_model(self):
        sem = SemanticMemory()
        sem.model = None  # 强制不可用
        sem._notes = [{"text": "test", "note_index": 1}]
        results = sem.search("test")
        assert results == []


class TestContextManagerEdge:
    """context_manager.py 边界测试。"""

    def test_build_with_all_sections_empty(self, temp_workspace):
        """仅 prefix + request，其他 section 空。"""
        ws = WorkspaceContext.build(str(temp_workspace))
        agent = Agent(
            config=AgentConfig(provider="fake", max_steps=2),
            model_client=FakeModelClient(["<final>ok</final>"]),
            workspace=ws,
        )
        cm = ContextManager(agent)
        prompt, meta = cm.build("hello")
        assert "当前任务" in prompt
        assert meta["total_tokens"] > 0

    def test_remaining_budget(self):
        budget = TokenBudget(total_limit=100)
        assert budget.remaining(30) == 70
        assert budget.remaining(120) == 0

    def test_fit_no_truncation_needed(self):
        budget = TokenBudget()
        text = "short"
        assert budget.fit(text, 100) == text

    def test_truncate_preserves_important_lines(self):
        content = "line1\nError: failed\nline2\n/path/to/file.py:42\nerror: not found\nz" * 5
        result = _truncate_tool_content(content, "read_file")
        assert "Error" in result
        assert ".py" in result

    def test_truncate_short_content_unchanged(self):
        text = "short result"
        result = _truncate_tool_content(text, "read_file")
        assert result == text

    def test_empty_history_no_error(self, temp_workspace):
        ws = WorkspaceContext.build(str(temp_workspace))
        agent = Agent(
            config=AgentConfig(provider="fake", max_steps=2),
            model_client=FakeModelClient(["<final>ok</final>"]),
            workspace=ws,
        )
        cm = ContextManager(agent)
        history = cm._get_compressed_history()
        assert history == ""
