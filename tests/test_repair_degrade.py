"""Multi-Agent → Single-Agent baseline 降级测试。"""

import json

import pytest

from agent_runtime.providers.clients import FakeModelClient
from agent_runtime.workspace import WorkspaceContext
from src.agents.factory import create_localizer, create_patcher
from src.orchestrator import Orchestrator
from src.repair.degrade import (
    build_degraded_baseline_prompt,
    should_degrade_to_baseline,
)
from src.repair.run_trace import RepairRunTracer
from src.state import RepairState, SuspectLocation, VerificationResult


class TestShouldDegrade:
    def test_triggers_after_verify_exhausted(self):
        state = RepairState(
            issue_input="bug",
            retry_count=3,
            max_retries=3,
            verification_result=VerificationResult(all_passed=False, failed=1),
        )
        assert should_degrade_to_baseline(
            state,
            verification_enabled=True,
            cancelled=False,
        )

    def test_skips_without_verify_failure(self):
        state = RepairState(issue_input="bug", retry_count=3, max_retries=3)
        assert not should_degrade_to_baseline(
            state,
            verification_enabled=True,
            cancelled=False,
        )

    def test_skips_when_fixed(self):
        state = RepairState(
            issue_input="bug",
            retry_count=3,
            max_retries=3,
            status="fixed",
            verification_result=VerificationResult(all_passed=False),
        )
        state.candidate_patches = [{}]  # type: ignore[list-item]
        assert not should_degrade_to_baseline(
            state,
            verification_enabled=True,
            cancelled=False,
        )

    def test_skips_when_cancelled(self):
        state = RepairState(
            issue_input="bug",
            retry_count=3,
            max_retries=3,
            verification_result=VerificationResult(all_passed=False),
        )
        state.node_timings["user_cancel"] = True
        assert not should_degrade_to_baseline(
            state,
            verification_enabled=True,
            cancelled=True,
        )

    def test_skips_without_verification(self):
        state = RepairState(
            issue_input="bug",
            retry_count=3,
            max_retries=3,
            verification_result=VerificationResult(all_passed=False),
        )
        assert not should_degrade_to_baseline(
            state,
            verification_enabled=False,
            cancelled=False,
        )

    def test_skips_when_allow_false(self):
        state = RepairState(
            issue_input="bug",
            retry_count=3,
            max_retries=3,
            verification_result=VerificationResult(all_passed=False, failed=1),
        )
        assert not should_degrade_to_baseline(
            state,
            verification_enabled=True,
            cancelled=False,
            allow=False,
        )


class TestDegradedPrompt:
    def test_includes_feedback_and_suspects(self):
        state = RepairState(
            issue_input="TypeError in calc.py",
            retry_count=2,
            feedback="test still fails",
            suspect_locations=[
                SuspectLocation(
                    file_path="calc.py",
                    start_line=1,
                    end_line=2,
                    reason="stack",
                )
            ],
        )
        prompt = build_degraded_baseline_prompt(state)
        assert "最后一搏" in prompt
        assert "calc.py" in prompt
        assert "test still fails" in prompt

    def test_includes_blackboard_blocks(self, temp_workspace):
        from src.blackboard import Blackboard
        from src.orchestrator import Orchestrator
        from src.repair.blackboard_merge import write_localize_phase_to_blackboard
        from src.repair.run_context import RepairRunContext

        (temp_workspace / "calc.py").write_text("x=1\n", encoding="utf-8")
        bb = Blackboard()
        write_localize_phase_to_blackboard(
            bb,
            [
                SuspectLocation(
                    file_path="calc.py",
                    start_line=1,
                    end_line=1,
                    reason="stack",
                )
            ],
            None,
        )
        orch = Orchestrator(None, None, None)
        orch._repo_root = str(temp_workspace)
        orch._repair_ctx = RepairRunContext(blackboard=bb)
        state = RepairState(
            issue_input="TypeError in calc.py",
            retry_count=2,
            suspect_locations=[],
        )
        prompt = build_degraded_baseline_prompt(state, orch)
        assert "嫌疑位置" in prompt
        assert "calc.py:1" in prompt


class DegradeTestClient(FakeModelClient):
    """Patcher complete_once 失败；baseline ask 成功。"""

    def __init__(self):
        super().__init__([])

    def complete(self, prompt: str, max_new_tokens: int = 512, prompt_cache_key: str = ""):
        if "最后一搏" in prompt:
            return (
                '<final>[{"file_path":"calc.py","original_lines":"    return a - b",'
                '"patched_lines":"    return a + b"}]</final>'
            )
        return (
            '[{"file_path":"calc.py","original_lines":"    return a - b",'
            '"patched_lines":"    return a - c","explanation":"wrong"}]'
        )


class TestRepairDegradeIntegration:
    def test_degrades_to_baseline_after_verify_exhausted(self, temp_workspace):
        (temp_workspace / "calc.py").write_text(
            "def add(a, b):\n    return a - b\n",
            encoding="utf-8",
        )
        (temp_workspace / "test_calc.py").write_text(
            "from calc import add\n\n\ndef test_add():\n    assert add(1, 2) == 3\n",
            encoding="utf-8",
        )
        ws = WorkspaceContext.build(str(temp_workspace))
        repo = str(temp_workspace.resolve())

        loc_client = FakeModelClient(
            [
                '<final>[{"file_path":"calc.py","start_line":1,"end_line":2,'
                '"function_name":"add","reason":"stack","confidence":0.9}]</final>',
            ]
        )
        loc = create_localizer(loc_client, ws, cwd=repo)
        pat = create_patcher(DegradeTestClient(), ws, cwd=repo)
        orch = Orchestrator(loc, None, pat, use_pytest_verify=True)
        orch._repo_root = repo

        state = orch.repair("TypeError in calc.py:2", max_retries=1)

        assert state.degraded_mode is True
        assert state.status == "fixed"
        assert state.node_timings.get("degraded_trigger") == "verify_exhausted"
        assert "return a + b" in (temp_workspace / "calc.py").read_text()

        tracer = RepairRunTracer(repo)
        trace_path = tracer.store.runs_dir / state.repair_run_id / "trace.jsonl"
        events = [
            json.loads(line)
            for line in trace_path.read_text(encoding="utf-8").strip().splitlines()
        ]
        assert any(e.get("event") == "repair_degraded_to_baseline" for e in events)
        assert any(e.get("event") == "baseline_verify_finished" for e in events)
        assert state.verification_result is not None
        assert state.verification_result.all_passed is True
        assert "baseline" in {ref.agent for ref in state.agent_asks}
        assert state.failure_tags == ["degraded_baseline"]

    def test_no_degrade_when_disabled(self, temp_workspace):
        (temp_workspace / "calc.py").write_text(
            "def add(a, b):\n    return a - b\n",
            encoding="utf-8",
        )
        (temp_workspace / "test_calc.py").write_text(
            "from calc import add\n\n\ndef test_add():\n    assert add(1, 2) == 3\n",
            encoding="utf-8",
        )
        ws = WorkspaceContext.build(str(temp_workspace))
        repo = str(temp_workspace.resolve())
        loc = create_localizer(
            FakeModelClient(
                [
                    '<final>[{"file_path":"calc.py","start_line":1,"end_line":2,'
                    '"function_name":"add","reason":"stack","confidence":0.9}]</final>',
                ]
            ),
            ws,
            cwd=repo,
        )
        pat = create_patcher(DegradeTestClient(), ws, cwd=repo)
        orch = Orchestrator(loc, None, pat, use_pytest_verify=True)
        orch._repo_root = repo

        state = orch.repair(
            "TypeError in calc.py:2",
            max_retries=1,
            allow_baseline_degrade=False,
        )

        assert state.degraded_mode is False
        assert state.status == "exhausted"

    def test_no_degrade_when_patch_never_reaches_verify(self, temp_workspace):
        (temp_workspace / "calc.py").write_text("x = 1\n", encoding="utf-8")

        class BadPatchClient(FakeModelClient):
            def complete(self, prompt, max_new_tokens=512, prompt_cache_key=""):
                if "最后一搏" in prompt:
                    pytest.fail("baseline should not run")
                return "not json"

        ws = WorkspaceContext.build(str(temp_workspace))
        repo = str(temp_workspace.resolve())
        loc = create_localizer(
            FakeModelClient(
                [
                    '<final>[{"file_path":"calc.py","start_line":1,"end_line":1,'
                    '"function_name":"","reason":"stack","confidence":0.9}]</final>',
                ]
            ),
            ws,
            cwd=repo,
        )
        pat = create_patcher(BadPatchClient([]), ws, cwd=repo)
        orch = Orchestrator(loc, None, pat, use_pytest_verify=True)
        orch._repo_root = repo

        state = orch.repair("error", max_retries=1)

        assert state.degraded_mode is False
        assert state.status == "exhausted"
