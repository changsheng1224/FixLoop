"""Orchestrator 集成测试：FakeClient 模拟完整修复流水线。"""

import pytest

from agent_runtime.providers.clients import FakeModelClient
from agent_runtime.workspace import WorkspaceContext
from src.agents.localizer import create_localizer
from src.agents.patcher import create_patcher
from src.agents.retriever import create_retriever
from src.orchestrator import Orchestrator, apply_patch_to_text
from src.state import CandidatePatch, VerificationResult


@pytest.fixture
def workspace(temp_workspace):
    return WorkspaceContext.build(str(temp_workspace))


class TestOrchestrator:
    def test_parse_type_error(self):
        loc = FakeModelClient(["<final>ok</final>"])
        ret = FakeModelClient(["<final>ok</final>"])
        pat = FakeModelClient(["<final>ok</final>"])
        orch = Orchestrator(
            create_localizer(loc, WorkspaceContext.build(".")),
            create_retriever(ret, WorkspaceContext.build(".")),
            create_patcher(pat, WorkspaceContext.build(".")),
        )
        plan = orch._parse_issue(
            'TypeError: unsupported operand at calculator.py:42'
        )
        assert plan.issue_type == "type_error"
        assert "calculator.py" in plan.suspect_files

    def test_parse_import_error(self):
        orch = Orchestrator(None, None, None)
        plan = orch._parse_issue(
            'ModuleNotFoundError: No module named "utils" at main.py:3'
        )
        assert plan.issue_type == "import_error"

    def test_full_pipeline_fake(self, temp_workspace):
        ws = WorkspaceContext.build(str(temp_workspace))
        # 3 个 Agent 都用 FakeClient 预设输出
        loc_client = FakeModelClient([
            '<final>[{"file_path":"calc.py","start_line":42,"end_line":44,"function_name":"add","reason":"堆栈指向","confidence":0.95}]</final>',
        ])
        ret_client = FakeModelClient([
            '<final>{"related_tests":["test_calc.py::test_add"]}</final>',
        ])
        pat_client = FakeModelClient([
            '<final>[{"file_path":"calc.py","diff":"-old\\n+new","explanation":"修复类型转换"}]</final>',
        ])

        orch = Orchestrator(
            create_localizer(loc_client, ws),
            create_retriever(ret_client, ws),
            create_patcher(pat_client, ws),
        )

        state = orch.repair("TypeError at calc.py:42")
        assert state.repair_plan.issue_type == "type_error"
        assert len(state.suspect_locations) == 1
        assert state.suspect_locations[0].file_path == "calc.py"
        assert len(state.candidate_patches) == 1
        assert state.status == "patched"
        assert "localizer_ms" in state.node_timings
        assert "patcher_ms" in state.node_timings


class TestApplyPatch:
    def test_apply_diff_preserves_indent(self):
        text = "def add(a, b):\n    return a + b  # BUG\n"
        patch = CandidatePatch(
            file_path="calculator.py",
            diff="-return a + b\n+return int(a) + int(b)",
        )
        result = apply_patch_to_text(text, patch)
        assert result is not None
        assert "return int(a) + int(b)" in result
        assert "    return int(a)" in result

    def test_apply_diff_with_inline_comment(self):
        text = "def add(a, b):\n    return a + b  # BUG\n"
        patch = CandidatePatch(
            file_path="calculator.py",
            diff="-return a + b  # BUG\n+return int(a) + int(b)",
        )
        result = apply_patch_to_text(text, patch)
        assert result is not None
        assert "return int(a) + int(b)" in result

    def test_apply_original_lines_by_strip(self):
        text = "    return a + b\n"
        patch = CandidatePatch(
            file_path="x.py",
            original_lines="return a + b",
            patched_lines="return int(a) + int(b)",
        )
        result = apply_patch_to_text(text, patch)
        assert result == "    return int(a) + int(b)\n"
