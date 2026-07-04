"""Orchestrator 集成测试：FakeClient 模拟完整修复流水线。"""

from agent_runtime.providers.clients import FakeModelClient
from agent_runtime.workspace import WorkspaceContext
from src.agents.localizer import create_localizer
from src.agents.patcher import create_patcher
from src.agents.retriever import create_retriever
from src.orchestrator import Orchestrator, apply_patch_to_text
from src.state import CandidatePatch, RepairPlan, SuspectLocation


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
        plan = orch._parse_issue("TypeError: unsupported operand at calculator.py:42")
        assert plan.issue_type == "type_error"
        assert "calculator.py" in plan.suspect_files

    def test_parse_import_error(self):
        orch = Orchestrator(None, None, None)
        plan = orch._parse_issue('ModuleNotFoundError: No module named "utils" at main.py:3')
        assert plan.issue_type == "import_error"
        assert plan.reasoning == "main.py:3"

    def test_fallback_suspects_import_line(self, temp_workspace):
        (temp_workspace / "app.py").write_text(
            "from utils.helper import greet\n\ndef main():\n    return greet()\n",
            encoding="utf-8",
        )
        orch = Orchestrator(None, None, None)
        orch._repo_root = str(temp_workspace)
        plan = RepairPlan(
            issue_type="import_error",
            suspect_files=["app.py"],
            reasoning="app.py:3",
        )
        suspects = orch._fallback_suspects_from_plan(
            plan,
            "ModuleNotFoundError at app.py:3",
        )
        assert len(suspects) == 1
        assert suspects[0].start_line == 1
        assert suspects[0].file_path == "app.py"

    def test_patcher_rejects_outside_repo(self, temp_workspace):
        (temp_workspace / "app.py").write_text("x = 1\n", encoding="utf-8")
        orch = Orchestrator(None, None, None)
        orch._repo_root = str(temp_workspace)
        patches = [
            CandidatePatch(file_path="calculator.py", diff="-x\n+y"),
            CandidatePatch(
                file_path="app.py",
                original_lines="x = 1",
                patched_lines="x = 2",
            ),
        ]
        applied = orch._apply_patches_on_disk(patches)
        assert len(applied) == 1
        assert applied[0].file_path == "app.py"
        assert (temp_workspace / "app.py").read_text(encoding="utf-8") == "x = 2\n"

    def test_patcher_prompt_discovers_test_app(self, temp_workspace):
        (temp_workspace / "app.py").write_text(
            "from utils.helper import greet\n",
            encoding="utf-8",
        )
        (temp_workspace / "test_app.py").write_text(
            "def test_main():\n    from app import main\n",
            encoding="utf-8",
        )
        orch = Orchestrator(None, None, None)
        orch._repo_root = str(temp_workspace)
        plan = RepairPlan(issue_type="import_error", suspect_files=["app.py"])
        prompt = orch._patcher_prompt([], None, plan=plan, issue="ModuleNotFoundError at app.py:1")
        assert "test_app.py" in prompt
        assert "utils.helper" in prompt

    def test_full_pipeline_fake(self, temp_workspace):
        (temp_workspace / "calc.py").write_text("old\n", encoding="utf-8")
        ws = WorkspaceContext.build(str(temp_workspace))
        # 3 个 Agent 都用 FakeClient 预设输出
        loc_client = FakeModelClient(
            [
                '<final>[{"file_path":"calc.py","start_line":42,"end_line":44,"function_name":"add","reason":"堆栈指向","confidence":0.95}]</final>',
            ]
        )
        ret_client = FakeModelClient(
            [
                '<final>{"related_tests":["test_calc.py::test_add"]}</final>',
            ]
        )
        pat_client = FakeModelClient(
            [
                '<final>[{"file_path":"calc.py","diff":"-old\\n+new","explanation":"修复类型转换"}]</final>',
            ]
        )

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
        assert "retriever_ms" in state.node_timings
        assert "localize_retrieve_ms" in state.node_timings
        assert "patcher_ms" in state.node_timings
        assert state.node_timings.get("total_tokens", 0) > 0
        assert "token_usage" in state.node_timings

    def test_retriever_prompt_from_plan(self):
        orch = Orchestrator(None, None, None)
        plan = RepairPlan(language="python", suspect_files=["calculator.py"])
        prompt = orch._retriever_prompt([], plan=plan, issue="TypeError at calculator.py:6")
        assert "calculator.py" in prompt
        assert "find_test" in prompt.lower() or "find_test" in prompt

    def test_patcher_prompt_includes_test_file(self, temp_workspace):
        repo = temp_workspace
        (repo / "calculator.py").write_text(
            "def add(a, b):\n    return a + b\n",
            encoding="utf-8",
        )
        (repo / "test_calculator.py").write_text(
            'def test_add_str():\n    assert add("3", 2) == 5\n',
            encoding="utf-8",
        )
        orch = Orchestrator(None, None, None)
        orch._repo_root = str(repo)
        suspects = [
            SuspectLocation(
                file_path="calculator.py",
                start_line=2,
                end_line=2,
                function_name="add",
                reason="堆栈指向",
            ),
        ]
        plan = RepairPlan(issue_type="type_error")
        prompt = orch._patcher_prompt(
            suspects,
            None,
            plan=plan,
            issue="TypeError: concatenate str",
        )
        assert 'assert add("3", 2) == 5' in prompt
        assert "int()" in prompt or "数值转换" in prompt


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

    def test_apply_import_module_fallback(self):
        text = "from utils.helper import greet  # BUG: 模块名为 helpers\n"
        patch = CandidatePatch(
            file_path="app.py",
            diff="-from utils.helpers import greet\n+from utils.helpers import greet",
        )
        result = apply_patch_to_text(text, patch)
        assert result is not None
        assert "from utils.helpers import greet" in result

    def test_sync_import_symbol_usages(self):
        from src.repair.patch_applier import _sync_import_symbol_usages

        old = "from utils.helpers import hello\n\ndef message():\n    return hello()\n"
        new = "from utils.helpers import greet\n\ndef message():\n    return hello()\n"
        patch = CandidatePatch(
            file_path="service.py",
            diff="-from utils.helpers import hello\n+from utils.helpers import greet",
        )
        result = _sync_import_symbol_usages(old, new, patch)
        assert "return greet()" in result

    def test_parse_issue_composite_and_source_files(self):
        orch = Orchestrator(None, None, None)
        issue = (
            "ModuleNotFoundError + TypeError (composite)\n"
            'File "gateway.py", line 3\n'
            "Candidate source files: gateway.py, backend/tasks.py"
        )
        plan = orch._parse_issue(issue)
        assert plan.issue_type == "composite"
        assert "gateway.py" in plan.suspect_files
        assert "backend/tasks.py" in plan.suspect_files

    def test_snapshot_restore(self, temp_workspace):
        orch = Orchestrator(None, None, None)
        orch._repo_root = str(temp_workspace)
        (temp_workspace / "a.py").write_text("old\n", encoding="utf-8")
        snap = orch._snapshot_repo()
        (temp_workspace / "a.py").write_text("new\n", encoding="utf-8")
        orch._restore_repo_snapshot(snap)
        assert (temp_workspace / "a.py").read_text(encoding="utf-8") == "old\n"

    def test_restore_clears_pycache(self, temp_workspace):
        orch = Orchestrator(None, None, None)
        orch._repo_root = str(temp_workspace)
        (temp_workspace / "a.py").write_text("old\n", encoding="utf-8")
        cache_dir = temp_workspace / "__pycache__"
        cache_dir.mkdir()
        (cache_dir / "a.cpython-313.pyc").write_bytes(b"stale")
        snap = orch._snapshot_repo()
        (temp_workspace / "a.py").write_text("new\n", encoding="utf-8")
        orch._restore_repo_snapshot(snap)
        assert not cache_dir.exists()

    def test_pytest_verify_retries_on_failure(self, temp_workspace):
        (temp_workspace / "foo.py").write_text("x = 1\n", encoding="utf-8")
        (temp_workspace / "test_foo.py").write_text(
            "from foo import x\n\n\ndef test_x():\n    assert x == 2\n",
            encoding="utf-8",
        )
        ws = WorkspaceContext.build(str(temp_workspace))
        repo = str(temp_workspace.resolve())

        class SeqPatchClient(FakeModelClient):
            def __init__(self):
                self._step = 0

            def complete(self, prompt, max_new_tokens=4096):
                self._step += 1
                if self._step == 1:
                    return '[{"file_path":"foo.py","original_lines":"x = 1","patched_lines":"x = 3","explanation":"wrong"}]'
                return '[{"file_path":"foo.py","original_lines":"x = 1","patched_lines":"x = 2","explanation":"fix"}]'

        pat = create_patcher(SeqPatchClient(), ws, cwd=repo)
        orch = Orchestrator(None, None, pat, use_pytest_verify=True)
        orch._repo_root = repo
        state = orch.repair('File "foo.py", line 1\nassert x == 2')
        assert state.status == "fixed"
        assert state.retry_count == 1
        assert (temp_workspace / "foo.py").read_text(encoding="utf-8") == "x = 2\n"
