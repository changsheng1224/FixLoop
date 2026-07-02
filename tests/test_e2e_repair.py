"""M6 端到端边界测试：FakeClient 模拟完整闭环的 3 种边界情况。"""

import pytest

from agent_runtime.providers.clients import FakeModelClient
from agent_runtime.workspace import WorkspaceContext
from src.agents.localizer import create_localizer
from src.agents.patcher import create_patcher
from src.agents.retriever import create_retriever
from src.agents.verifier import create_verifier
from src.orchestrator import Orchestrator


@pytest.fixture
def ws(temp_workspace):
    return WorkspaceContext.build(str(temp_workspace))


class TestSelfHealing:
    """自愈循环：Patcher 被调用 2 次。"""

    def test_retry_on_first_failure(self, ws):
        loc = FakeModelClient([
            '<final>[{"file_path":"calc.py","start_line":6,"end_line":6,"function_name":"add","reason":"堆栈指向","confidence":0.95}]</final>',
        ])
        ret = FakeModelClient([
            '<final>{"related_tests":["test_calc.py::test_add"]}</final>',
        ])
        # Patcher: 第 1 次不完整补丁, 第 2 次完整
        pat = FakeModelClient([
            '<final>[{"file_path":"calc.py","diff":"incomplete","explanation":"不完整修复"}]</final>',
            '<final>[{"file_path":"calc.py","diff":"complete","explanation":"完整修复"}]</final>',
        ])
        ver = FakeModelClient([
            '<final>{"all_passed":false,"total_tests":4,"passed":3,"failed":1,"failure_logs":["test_add_str"]}</final>',
            '<final>{"all_passed":true,"total_tests":4,"passed":4,"failed":0,"failure_logs":[]}</final>',
        ])

        orch = Orchestrator(
            create_localizer(loc, ws),
            create_retriever(ret, ws),
            create_patcher(pat, ws),
            create_verifier(ver, ws),
        )
        state = orch.repair("TypeError at calc.py:6")
        assert state.status == "fixed"
        assert state.retry_count == 1  # 重试了 1 次


class TestEdgeCases:
    def test_no_verifier_fallback(self, ws):
        """无 Verifier 时 status=patched。"""
        loc = FakeModelClient(["<final>[{}]</final>"])
        ret = FakeModelClient(["<final>{}</final>"])
        pat = FakeModelClient(["<final>[{}]</final>"])

        orch = Orchestrator(
            create_localizer(loc, ws),
            create_retriever(ret, ws),
            create_patcher(pat, ws),
            verifier=None,
        )
        state = orch.repair("test")
        assert state.status == "patched"

    def test_build_feedback_format(self):
        """_build_feedback 格式化失败日志。"""
        from src.state import VerificationResult
        orch = Orchestrator(None, None, None)
        result = VerificationResult(
            all_passed=False, total_tests=4, passed=3, failed=1,
            failure_logs=["test_add: AssertionError: assert 3 == 5"],
        )
        feedback = orch._build_feedback(result)
        assert "test_add" in feedback
        assert "请修改补丁" in feedback
