"""歧义 LLM fallback 单测：unknown→LLM 分类 / 失败保持 unknown。"""


class TestLLMClassifyIssue:
    def test_unknown_triggers_llm_classify(self):
        """issue_type='unknown' 时调用 _llm_classify_issue。"""
        from unittest.mock import MagicMock, patch

        from src.orchestrator import Orchestrator

        orch = Orchestrator.__new__(Orchestrator)
        orch._light_client = MagicMock()
        orch._light_client.complete.return_value = "type_error"

        with patch.object(Orchestrator, "_llm_classify_issue", wraps=orch._llm_classify_issue):
            result = orch._llm_classify_issue("some unknown error description")
        assert result == "type_error"

    def test_no_light_client_returns_none(self):
        """无 light_client 时返回 None → 保持 unknown。"""
        from src.orchestrator import Orchestrator

        orch = Orchestrator.__new__(Orchestrator)
        orch._light_client = None

        result = orch._llm_classify_issue("error")
        assert result is None

    def test_bad_response_returns_none(self):
        """LLM 返回非法类型 → None → 保持 unknown。"""
        from unittest.mock import MagicMock

        from src.orchestrator import Orchestrator

        orch = Orchestrator.__new__(Orchestrator)
        orch._light_client = MagicMock()
        orch._light_client.complete.return_value = "not_a_valid_type"

        result = orch._llm_classify_issue("error")
        assert result is None

    def test_exception_returns_none(self):
        """LLM 调用异常 → None → 不阻塞主路径。"""
        from unittest.mock import MagicMock

        from src.orchestrator import Orchestrator

        orch = Orchestrator.__new__(Orchestrator)
        orch._light_client = MagicMock()
        orch._light_client.complete.side_effect = RuntimeError("API error")

        result = orch._llm_classify_issue("error")
        assert result is None

    def test_classify_includes_all_types(self):
        """分类 prompt 包含所有 ROUTED_ISSUE_TYPES。"""
        from unittest.mock import MagicMock

        from src.orchestrator import ROUTED_ISSUE_TYPES, Orchestrator

        orch = Orchestrator.__new__(Orchestrator)
        orch._light_client = MagicMock()
        orch._light_client.complete.return_value = "type_error"

        orch._llm_classify_issue("test")
        prompt = orch._light_client.complete.call_args[0][0]
        for t in ROUTED_ISSUE_TYPES:
            assert t in prompt


class TestParseIssueFallback:
    def test_unknown_gets_llm_type_and_intent(self):
        """_parse_issue: unknown → LLM 分类成功 → intent_parser='llm'。"""
        from unittest.mock import MagicMock, patch

        from src.orchestrator import Orchestrator

        orch = Orchestrator.__new__(Orchestrator)
        orch._light_client = MagicMock()
        orch._light_client.complete.return_value = "type_error"

        # mock _classify_issue_type to return "unknown"
        orch._classify_issue_type = lambda i: ("unknown", "none")
        orch._repo_root = "."

        with patch.object(orch, "_parse_file_line", return_value=0):
            plan = orch._parse_issue("some ambiguous error")

        assert plan.issue_type == "type_error"
        assert plan.intent_parser == "llm"

    def test_parse_issue_unknown_without_light_client(self):
        """_parse_issue: unknown + 无 light_client → intent_parser='rule'。"""
        from unittest.mock import patch

        from src.orchestrator import Orchestrator

        orch = Orchestrator.__new__(Orchestrator)
        orch._light_client = None
        orch._classify_issue_type = lambda i: ("unknown", "none")
        orch._repo_root = "."

        with patch.object(orch, "_parse_file_line", return_value=0):
            plan = orch._parse_issue("ambiguous error")

        assert plan.issue_type == "unknown"
        assert plan.intent_parser == "rule"

    def test_parse_issue_known_type_skips_llm(self):
        """_parse_issue: 规则匹配到已知类型 → 不调 LLM。"""
        from unittest.mock import MagicMock, patch

        from src.orchestrator import Orchestrator

        orch = Orchestrator.__new__(Orchestrator)
        orch._light_client = MagicMock()
        orch._classify_issue_type = lambda i: ("type_error", "explicit_exception")
        orch._repo_root = "."

        with patch.object(orch, "_parse_file_line", return_value=0):
            plan = orch._parse_issue("TypeError at calc.py:42")

        assert plan.issue_type == "type_error"
        # 已知类型不调 LLM → light_client.complete 不应被调用
        orch._light_client.complete.assert_not_called()
