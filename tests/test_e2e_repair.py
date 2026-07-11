"""M6 端到端边界测试：FakeClient 模拟完整闭环的 3 种边界情况。"""

from unittest.mock import MagicMock

from agent_runtime.providers.clients import FakeModelClient
from src.agents.localizer import create_localizer
from src.agents.patcher import create_patcher
from src.agents.retriever import create_retriever
from src.orchestrator import Orchestrator
from src.state import VerificationResult


class TestSelfHealing:
    """自愈循环：Patcher 被调用 2 次。"""

    def test_retry_on_first_failure(self, ws, temp_workspace):
        (temp_workspace / "calc.py").write_text("return a + b\n", encoding="utf-8")
        loc = FakeModelClient(
            [
                '<final>[{"file_path":"calc.py","start_line":6,"end_line":6,"function_name":"add","reason":"堆栈指向","confidence":0.95}]</final>',
            ]
        )
        ret = FakeModelClient(
            [
                '<final>{"related_tests":["test_calc.py::test_add"]}</final>',
            ]
        )
        # Patcher: 第 1 次错误补丁, 第 2 次正确补丁（Orchestrator 直接调 complete）
        pat = FakeModelClient(
            [
                '[{"file_path":"calc.py","original_lines":"return a + b","patched_lines":"return str(a) + str(b)","explanation":"不完整修复"}]',
                '[{"file_path":"calc.py","original_lines":"return a + b","patched_lines":"return int(a) + int(b)","explanation":"完整修复"}]',
            ]
        )

        orch = Orchestrator(
            create_localizer(loc, ws, cwd=str(temp_workspace)),
            create_retriever(ret, ws, cwd=str(temp_workspace)),
            create_patcher(pat, ws, cwd=str(temp_workspace)),
            verifier=MagicMock(),
        )
        orch._repo_root = str(temp_workspace)
        orch._run_verifier = MagicMock(
            side_effect=[
                VerificationResult(
                    all_passed=False,
                    total_tests=4,
                    passed=3,
                    failed=1,
                    failure_logs=["test_add_str: AssertionError"],
                ),
                VerificationResult(
                    all_passed=True,
                    total_tests=4,
                    passed=4,
                    failed=0,
                ),
            ]
        )
        state = orch.repair("TypeError at calc.py:6")
        assert state.status == "fixed"
        assert state.retry_count == 1  # 重试了 1 次


class TestEdgeCases:
    def test_no_verifier_fallback(self, ws, temp_workspace):
        """无 Verifier 且补丁成功应用时 status=fixed + verify_skipped。"""
        (temp_workspace / "calc.py").write_text("x = 1\n", encoding="utf-8")
        loc = FakeModelClient(
            [
                '<final>[{"file_path":"calc.py","start_line":1,"end_line":1,'
                '"reason":"堆栈","confidence":0.9}]</final>',
            ]
        )
        ret = FakeModelClient(['<final>{"related_tests":[]}</final>'])
        pat = FakeModelClient(
            [
                '[{"file_path":"calc.py","original_lines":"x = 1",'
                '"patched_lines":"x = 2","explanation":"fix"}]',
            ]
        )

        orch = Orchestrator(
            create_localizer(loc, ws, cwd=str(temp_workspace)),
            create_retriever(ret, ws, cwd=str(temp_workspace)),
            create_patcher(pat, ws, cwd=str(temp_workspace)),
            verifier=None,
        )
        orch._repo_root = str(temp_workspace)
        state = orch.repair("test")
        assert state.status == "fixed"
        assert state.node_timings.get("verify_skipped") is True

    def test_build_feedback_format(self):
        """_build_feedback 格式化失败日志。"""
        from src.state import VerificationResult

        orch = Orchestrator(None, None, None)
        result = VerificationResult(
            all_passed=False,
            total_tests=4,
            passed=3,
            failed=1,
            failure_logs=["test_add: AssertionError: assert 3 == 5"],
        )
        feedback = orch._build_feedback(result)
        assert "test_add" in feedback
        assert "修改补丁" in feedback

    def test_verifier_build_failure_retries(self, ws, temp_workspace):
        """构建/验证失败时不应标记 fixed，应进入重试。"""
        (temp_workspace / "calc.py").write_text("x = 1\n")
        orch = self._orch_with_patches(ws, temp_workspace)
        orch._run_verifier = MagicMock(
            return_value=VerificationResult(
                all_passed=False,
                total_tests=0,
                passed=0,
                failed=0,
                build_log="pip install: exit_code=1\nERROR: setup failed",
                failure_logs=["build failed"],
            )
        )
        state = orch.repair("TypeError at calc.py:1", max_retries=1)
        assert state.status != "fixed"
        assert state.verification_result is not None
        assert not state.verification_result.all_passed

    def test_verifier_all_passed_marks_fixed(self, ws, temp_workspace):
        """全部测试通过时 status=fixed。"""
        (temp_workspace / "calc.py").write_text("x = 1\n")
        orch = self._orch_with_patches(ws, temp_workspace)
        orch._run_verifier = MagicMock(
            return_value=VerificationResult(
                all_passed=True,
                total_tests=4,
                passed=4,
                failed=0,
            )
        )
        state = orch.repair("TypeError at calc.py:1")
        assert state.status == "fixed"

    def test_verifier_exception_recorded(self, ws, temp_workspace, monkeypatch):
        """Verifier 异常写入 agent_errors，不崩溃。"""
        (temp_workspace / "calc.py").write_text("x = 1\n")
        orch = self._orch_with_patches(ws, temp_workspace)

        monkeypatch.setattr(
            "src.harness.sandbox_verify.assert_sandbox_available",
            lambda: None,
        )

        def _boom(*_a, **_kw):
            raise RuntimeError("sandbox timeout")

        monkeypatch.setattr(
            "src.tools.sandbox_tools.run_sandbox_verification",
            _boom,
        )
        state = orch.repair("TypeError at calc.py:1", max_retries=1)
        assert "verifier" in state.agent_errors
        assert "sandbox timeout" in state.agent_errors["verifier"]
        assert state.status != "fixed"
        assert state.verification_result is not None
        assert not state.verification_result.all_passed

    @staticmethod
    def _orch_with_patches(ws, temp_workspace):
        loc = FakeModelClient(
            [
                '<final>[{"file_path":"calc.py","start_line":1,"end_line":1,'
                '"reason":"堆栈","confidence":0.9}]</final>',
            ]
        )
        ret = FakeModelClient(['<final>{"related_tests":[]}</final>'])
        pat = FakeModelClient(
            [
                '<final>[{"file_path":"calc.py","original_lines":"x = 1",'
                '"patched_lines":"x = 2","explanation":"fix"}]</final>',
            ]
        )
        orch = Orchestrator(
            create_localizer(loc, ws),
            create_retriever(ret, ws),
            create_patcher(pat, ws),
            verifier=MagicMock(),
        )
        orch._repo_root = str(temp_workspace)
        return orch
