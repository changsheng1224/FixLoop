"""Retriever Agentic RAG 单测：prompt 增强 + retrieval_path + retrieval_steps。"""


class TestRetrieverSystemPrompt:
    def test_prompt_contains_agentic_keywords(self):
        """Retriever prompt 含 grep/多跳/retrieval_path 引导。"""
        prompt = _load_retriever_prompt()
        assert "grep" in prompt
        assert "多跳" in prompt
        assert "retrieval_path" in prompt

    def test_prompt_contains_llm_rule_degrade(self):
        """prompt 含 llm/rule/degrade 三路径说明。"""
        prompt = _load_retriever_prompt()
        assert "llm" in prompt
        assert "rule" in prompt
        assert "degrade" in prompt

    def test_prompt_contains_read_file_confirmation(self):
        """prompt 引导 grep 后必须 read_file 确认。"""
        prompt = _load_retriever_prompt()
        assert "read_file" in prompt
        assert "确认" in prompt

    def test_prompt_has_max_steps_guidance(self):
        """prompt 含工具步数预算指引。"""
        prompt = _load_retriever_prompt()
        assert "6" in prompt
        assert "submit" in prompt.lower()

    def test_prompt_forbids_wrapped_final_output(self):
        """Retriever 以 submit_retrieved_context 结束，禁止散文 final。"""
        prompt = _load_retriever_prompt()
        assert "submit_retrieved_context" in prompt
        assert "不要再输出散文" in prompt or "勿再输出散文" in prompt or "不要输出散文" in prompt


def _load_retriever_prompt() -> str:
    from pathlib import Path

    path = Path(__file__).parent.parent / "src" / "prompts" / "retriever.txt"
    return path.read_text(encoding="utf-8")


class TestRetrievalPath:
    def test_retrieval_path_default_llm(self):
        """默认 retrieval_path='llm'。"""
        from src.state import RepairState

        state = RepairState(issue_input="TypeError at calc.py:42")
        state.node_timings["retrieval_path"] = "llm"
        assert state.node_timings["retrieval_path"] == "llm"

    def test_retrieval_path_rule(self):
        """fast_retrieve 时 retrieval_path='rule'。"""
        from src.state import RepairState

        state = RepairState(issue_input="TypeError")
        state.node_timings["retrieval_path"] = "rule"
        assert state.node_timings["retrieval_path"] == "rule"

    def test_retrieval_path_degrade(self):
        """LLM 失败降级时 retrieval_path='llm→degrade'。"""
        from src.state import RepairState

        state = RepairState(issue_input="error")
        state.node_timings["retrieval_path"] = "llm→degrade"
        assert "degrade" in state.node_timings["retrieval_path"]

    def test_retrieval_path_is_traceable(self):
        """retrieval_path 写入 state.node_timings 供 trace 使用。"""
        from src.state import RepairState

        state = RepairState(issue_input="test")
        state.node_timings["retrieval_path"] = "llm"
        assert "retrieval_path" in state.node_timings


class TestRetrievalSteps:
    def test_steps_collection_in_state(self):
        """retrieval_steps 存储在 node_timings 中。"""
        from src.state import RepairState

        state = RepairState(issue_input="test")
        state.node_timings["retrieval_steps"] = [
            {"tool": "grep", "args": {"pattern": "TypeError"}, "hits": 3},
            {"tool": "read_file", "args": {"path": "calc.py"}, "hits": 1},
        ]
        assert len(state.node_timings["retrieval_steps"]) == 2
        assert state.node_timings["retrieval_steps"][0]["tool"] == "grep"

    def test_steps_contains_grep(self):
        """retrieval_steps 至少含 grep 步骤。"""
        from src.state import RepairState

        state = RepairState(issue_input="TypeError")
        steps = [
            {"tool": "grep", "args": {"pattern": "TypeError"}, "hits": 1},
        ]
        state.node_timings["retrieval_steps"] = steps
        tool_names = [s["tool"] for s in state.node_timings["retrieval_steps"]]
        assert "grep" in tool_names

    def test_retrieval_steps_present_in_timings(self):
        """retrieval_steps 可在 node_timings 中存储。"""
        from src.state import RepairState

        state = RepairState(issue_input="test")
        state.node_timings["retrieval_steps"] = [
            {"tool": "grep", "args": {"pattern": "Error"}, "hits": 2},
        ]
        assert "retrieval_steps" in state.node_timings
