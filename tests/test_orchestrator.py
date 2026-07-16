"""Orchestrator 集成测试：FakeClient 模拟完整修复流水线。"""

from agent_runtime.providers.clients import FakeModelClient, FakeNativeToolClient
from agent_runtime.workspace import WorkspaceContext
from src.agents.localizer import create_localizer
from src.agents.patcher import create_patcher
from src.agents.retriever import create_retriever
from src.orchestrator import Orchestrator, apply_patch_to_text
from src.state import CandidatePatch, RepairPlan, RepairState, SuspectLocation, VerificationResult


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
        assert plan.language == "python"
        assert plan.prompt_variants == {
            "patcher": "type_error",
            "localizer": "stack_first",
        }
        assert "calculator.py" in plan.suspect_files

    def test_parse_java_language(self):
        orch = Orchestrator(None, None, None)
        issue = "java.lang.NullPointerException at Bar.java:10"
        plan = orch._parse_issue(issue)
        assert plan.language == "java"
        assert plan.prompt_variants["patcher"] == "default"

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
        prompt, _ = orch._patcher_prompt(
            [], None, plan=plan, issue="ModuleNotFoundError at app.py:1"
        )
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
        assert state.status == "fixed"
        assert state.node_timings.get("verify_skipped") is True
        phases = state.node_timings.get("phases", {})
        assert phases.get("localize_ms", 0) > 0
        assert phases.get("retrieve_ms", 0) > 0
        assert phases.get("patch_ms", 0) > 0
        assert phases.get("repair_total_ms", 0) > 0
        assert "localizer_ms" in state.node_timings
        assert "localize_retrieve_ms" in state.node_timings
        assert "patcher_ms" in state.node_timings
        assert state.node_timings.get("total_tokens", 0) > 0
        assert "token_usage" in state.node_timings

    def test_localizer_only_uses_complete_once_and_recovers_tool_call_path(
        self, temp_workspace
    ):
        ws = WorkspaceContext.build(str(temp_workspace))
        loc_client = FakeModelClient(
            [
                "<function_calls>"
                '<invoke name="inspect_file">'
                '<parameter name="path">calc.py</parameter>'
                "</invoke>"
                "</function_calls>",
            ]
        )
        localizer = create_localizer(loc_client, ws)

        def fail_if_agent_loop_is_used(*args, **kwargs):
            raise AssertionError("localizer should use complete_once in L2 pipeline")

        localizer.ask = fail_if_agent_loop_is_used
        orch = Orchestrator(localizer, None, None)
        state = RepairState(
            issue_input="TypeError at calc.py:42",
            repair_plan=RepairPlan(issue_type="type_error"),
        )

        suspects, context, loc_timing, ret_timing = orch._run_localizer_only(state)

        assert [s.file_path for s in suspects] == ["calc.py"]
        assert context.related_tests == []
        assert loc_timing["internal"]["mode"] == "complete_once"
        assert ret_timing["total_ms"] == 0
        assert len(loc_client.prompts) == 1

    def test_repair_unified_trace_and_per_agent_tokens(self, temp_workspace):
        import json

        (temp_workspace / "calc.py").write_text("old\n", encoding="utf-8")
        ws = WorkspaceContext.build(str(temp_workspace))
        loc_client = FakeNativeToolClient(
            [
                '<final>[{"file_path":"calc.py","start_line":42,"end_line":44,'
                '"function_name":"add","reason":"堆栈指向","confidence":0.95}]</final>',
            ]
        )
        ret_client = FakeNativeToolClient(
            ['<final>{"related_tests":["test_calc.py::test_add"]}</final>']
        )
        pat_client = FakeModelClient(
            ['<final>[{"file_path":"calc.py","diff":"-old\\n+new","explanation":"fix"}]</final>']
        )
        orch = Orchestrator(
            create_localizer(loc_client, ws),
            create_retriever(ret_client, ws),
            create_patcher(pat_client, ws),
        )
        state = orch.repair("TypeError at calc.py:42")
        from agent_runtime.run_ids import is_valid_run_id

        assert is_valid_run_id(state.repair_run_id)

        runs_dir = temp_workspace / ".agent" / "runs" / state.repair_run_id
        assert (runs_dir / "trace.jsonl").is_file()
        assert (runs_dir / "report.json").is_file()

        agents_seen = set()
        events = []
        for line in (runs_dir / "trace.jsonl").read_text(encoding="utf-8").strip().splitlines():
            rec = json.loads(line)
            events.append(rec)
            agent = rec.get("payload", {}).get("agent")
            if agent:
                agents_seen.add(agent)
        assert "localizer" in agents_seen
        assert "retriever" in agents_seen
        assert "orchestrator" in agents_seen
        assert any(e.get("event") == "prompt_routing" for e in events)
        started = next(e for e in events if e.get("event") == "repair_started")
        l1_key = started.get("payload", {}).get("l1_prompt_cache_key")
        assert l1_key
        assert len(l1_key) == 64
        cache_keys = set()
        for rec in events:
            if rec.get("event") != "context_built":
                continue
            key = (rec.get("payload") or {}).get("prompt_cache_key") or (
                (rec.get("payload") or {}).get("prefix_hashes") or {}
            ).get("cache_key")
            if key:
                cache_keys.add(key)
        if cache_keys:
            assert cache_keys == {l1_key}

        report = json.loads((runs_dir / "report.json").read_text(encoding="utf-8"))
        assert report.get("phases", {}).get("repair_total_ms", 0) > 0
        assert report.get("repair_plan", {}).get("issue_type") == "type_error"
        assert report["repair_plan"]["prompt_variants"]["patcher"] == "type_error"
        by_agent = state.node_timings.get("token_usage_by_agent") or report.get(
            "token_usage_by_agent", {}
        )
        assert "localizer" in by_agent
        assert "retriever" in by_agent
        assert "patcher" in by_agent
        assert by_agent["localizer"]["total_tokens"] > 0
        assert by_agent["retriever"]["total_tokens"] > 0
        assert by_agent["patcher"]["total_tokens"] > 0
        tool_summary = report.get("tool_usage_by_agent") or state.node_timings.get(
            "tool_usage_by_agent", {}
        )
        assert "localizer" in tool_summary
        assert report.get("total_tool_steps") == sum(tool_summary.values())

    def test_repair_blackboard_trace_and_report(self, temp_workspace):
        import json

        (temp_workspace / "calc.py").write_text("old\n", encoding="utf-8")
        ws = WorkspaceContext.build(str(temp_workspace))
        loc_client = FakeModelClient(
            [
                '<final>[{"file_path":"calc.py","start_line":42,"end_line":44,'
                '"confidence":0.95}]</final>',
            ]
        )
        ret_client = FakeModelClient(
            ['<final>{"related_tests":["test_calc.py::test_add"]}</final>']
        )
        pat_client = FakeModelClient(
            ['<final>[{"file_path":"calc.py","diff":"-old\\n+new","explanation":"fix"}]</final>']
        )
        orch = Orchestrator(
            create_localizer(loc_client, ws),
            create_retriever(ret_client, ws),
            create_patcher(pat_client, ws),
        )
        state = orch.repair("TypeError at calc.py:42")
        assert state.blackboard_snapshot.get("entries")
        assert "suspect:calc.py:42" in state.blackboard_snapshot["entries"]

        runs_dir = temp_workspace / ".agent" / "runs" / state.repair_run_id
        events = [
            json.loads(line)
            for line in (runs_dir / "trace.jsonl").read_text(encoding="utf-8").strip().splitlines()
        ]
        event_names = {e.get("event") for e in events}
        assert "blackboard_written" in event_names
        assert "blackboard_merge_for_patch" in event_names
        assert "blackboard_prefix_subscribed" in event_names
        assert "blackboard_snapshot" in event_names

        report = json.loads((runs_dir / "report.json").read_text(encoding="utf-8"))
        assert report.get("blackboard_schema_version") == 1
        assert report.get("blackboard", {}).get("entries")

    def test_repair_routes_non_python_language_to_static_verifier(self, temp_workspace, monkeypatch):
        from src.repair import pipeline as pipeline_mod

        infos: list[str] = []

        def capture(msg, *args, **kwargs):
            infos.append(msg % args if args else str(msg))

        monkeypatch.setattr(pipeline_mod.log, "info", capture)

        (temp_workspace / "Bar.java").write_text("class Bar {}\n", encoding="utf-8")
        ws = WorkspaceContext.build(str(temp_workspace))
        orch = Orchestrator(
            create_localizer(FakeModelClient(["<final>[]</final>"]), ws),
            None,
            None,
        )
        state = orch.repair("java.lang.NullPointerException at Bar.java:10")
        assert state.repair_plan.language == "java"
        assert any("语言感知静态验证" in item for item in infos)

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
        prompt, _ = orch._patcher_prompt(
            suspects,
            None,
            plan=plan,
            issue="TypeError: concatenate str",
        )
        assert 'assert add("3", 2) == 5' in prompt
        from src.prompts.loader import load_role_prompt

        system = load_role_prompt("patcher", "type_error")
        assert "int(a)" in system or "TypeError" in system


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

    def test_apply_diff_uses_matching_lines_from_multifile_diff(self):
        text = (
            "import values\n"
            "def generate_report(name):\n"
            '    result = values.get_score(name) + " points"\n'
            "    return result\n"
        )
        patch = CandidatePatch(
            file_path="report.py",
            diff=(
                "--- a/report.py\n"
                "+++ b/report.py\n"
                "@@ -10,3 +10,3 @@ def generate_report(name):\n"
                '-    result = values.get_score(name) + " points"\n'
                '+    result = str(values.get_score(name)) + " points"\n'
                "--- a/values.py\n"
                "+++ b/values.py\n"
                "@@ -2,3 +2,3 @@ def get_score(name):\n"
                "-    return int(data[name])\n"
                "+    return int(data.get(name, '0') or 0)\n"
            ),
        )

        result = apply_patch_to_text(text, patch)

        assert result is not None
        assert 'result = str(values.get_score(name)) + " points"' in result

    def test_apply_diff_applies_multiple_separate_hunks(self):
        text = "a = 1\nkeep = True\nb = 2\n"
        patch = CandidatePatch(
            file_path="x.py",
            diff=(
                "--- a/x.py\n"
                "+++ b/x.py\n"
                "@@ -1,1 +1,1 @@\n"
                "-a = 1\n"
                "+a = 10\n"
                "@@ -3,1 +3,1 @@\n"
                "-b = 2\n"
                "+b = 20\n"
            ),
        )

        result = apply_patch_to_text(text, patch)

        assert result == "a = 10\nkeep = True\nb = 20\n"

    def test_apply_diff_falls_back_to_unique_return_statement(self):
        text = (
            "from values import clamp\n\n"
            "def normalize_score(score):\n"
            "    return clamp(score, 0, 100) / 100  # BUG\n"
        )
        patch = CandidatePatch(
            file_path="transform.py",
            diff="-return score / 100\n+return float(clamp(score, 0, 100))",
        )

        result = apply_patch_to_text(text, patch)

        assert result is not None
        assert "return float(clamp(score, 0, 100))" in result

    def test_apply_diff_replaces_unique_return_with_multiline_block(self):
        text = 'data = {"bob": "N/A"}\ndef get_score(name):\n    return int(data[name])\n'
        patch = CandidatePatch(
            file_path="values.py",
            diff=(
                "-return int(data.get(name, '0'))\n"
                "+raw = data.get(name, '0')\n"
                "+return int(raw) if str(raw).isdigit() else 0"
            ),
        )

        result = apply_patch_to_text(text, patch)

        assert result is not None
        assert "raw = data.get" in result
        assert "isdigit()" in result

    def test_apply_diff_falls_back_to_unique_assignment_statement(self):
        text = (
            "import values\n"
            "def generate_report(name):\n"
            '    result = values.get_score(name) + " points"\n'
            "    return result\n"
        )
        patch = CandidatePatch(
            file_path="report.py",
            diff=(
                '-result = get_score(name) + " points"\n'
                '+result = str(values.get_score(name)) + " points"'
            ),
        )

        result = apply_patch_to_text(text, patch)

        assert result is not None
        assert 'result = str(values.get_score(name)) + " points"' in result

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


# ---------------------------------------------------------------------------
# _parse_issue 规则补全（V1.4-Bonus7a）
# ---------------------------------------------------------------------------


class TestParseIssueTypeRules:
    """_classify_issue_type 规则链测试。"""

    def test_explicit_exception_type_error(self):
        issue_type, rule = Orchestrator._classify_issue_type(
            "File app.py line 42: TypeError: unsupported operand"
        )
        assert issue_type == "type_error"
        assert rule == "explicit_exception"

    def test_explicit_exception_import_error(self):
        issue_type, rule = Orchestrator._classify_issue_type(
            "ModuleNotFoundError: No module named 'utils'"
        )
        assert issue_type == "import_error"
        assert rule == "explicit_exception"

    def test_test_failure_pytest_output(self):
        issue_type, rule = Orchestrator._classify_issue_type(
            "FAILED test_app.py::test_add - AssertionError: assert 3 == 5"
        )
        assert issue_type == "test_failure"
        assert "test_failure" in rule

    def test_test_failure_assert_equals(self):
        issue_type, rule = Orchestrator._classify_issue_type(
            "AssertionError: assert x == 2  # x was 3"
        )
        assert issue_type == "test_failure"

    def test_composite_keyword(self):
        issue_type, rule = Orchestrator._classify_issue_type(
            "composite issue: both import and type errors"
        )
        assert issue_type == "composite"

    def test_config_error(self):
        issue_type, rule = Orchestrator._classify_issue_type(
            "pyproject.toml is missing [tool.pytest] section"
        )
        assert issue_type == "config_error"

    def test_logic_error_wrong_result(self):
        issue_type, rule = Orchestrator._classify_issue_type(
            "The divide function returns wrong result for negative numbers"
        )
        assert issue_type == "logic_error"

    def test_logic_error_should_return(self):
        issue_type, rule = Orchestrator._classify_issue_type(
            "validate_age should return False for invalid input"
        )
        assert issue_type == "logic_error"

    def test_logic_error_incorrect_output(self):
        issue_type, rule = Orchestrator._classify_issue_type(
            "get_label produces incorrect output when name is empty string"
        )
        assert issue_type == "logic_error"

    def test_logic_error_bug_in_function(self):
        issue_type, rule = Orchestrator._classify_issue_type(
            "There is a bug in the calculate_total function"
        )
        assert issue_type == "logic_error"

    def test_unknown_fallback(self):
        issue_type, rule = Orchestrator._classify_issue_type("help me please")
        assert issue_type == "unknown"
        assert rule == "none"

    def test_explicit_exception_beats_logic_error(self):
        """显式异常名优先于 logic_error 关键词。"""
        issue_type, rule = Orchestrator._classify_issue_type(
            "TypeError: the function returns incorrect result"
        )
        assert issue_type == "type_error"
        assert rule == "explicit_exception"

    def test_test_failure_beats_explicit_exception(self):
        """pytest FAILED 优先于异常名（test_failure 规则在 explicit_exception 之前）。"""
        issue_type, rule = Orchestrator._classify_issue_type(
            "FAILED tests/test_app.py::test_calc - assert 3 == 5"
        )
        assert issue_type == "test_failure"

    def test_parse_issue_sets_intent_parser(self):
        """_parse_issue 完整流程正确设置 intent_parser。"""
        orch = Orchestrator.__new__(Orchestrator)
        orch._repo_root = "."
        orch._light_client = None
        plan = orch._parse_issue("FAILED test_app.py::test_add - AssertionError: assert 3 == 5")
        assert plan.issue_type == "test_failure"
        assert plan.intent_parser == "rule:test_failure"

    def test_parse_issue_logic_error_intent(self):
        orch = Orchestrator.__new__(Orchestrator)
        orch._repo_root = "."
        orch._light_client = None
        plan = orch._parse_issue("the function returns wrong result for all negative inputs")
        assert plan.issue_type == "logic_error"
        assert plan.intent_parser == "rule:logic_error"


# ---------------------------------------------------------------------------
# _inject_repair_task_summary（V1.4-Bonus7b）
# ---------------------------------------------------------------------------


class TestInjectTaskSummary:
    def test_injects_summary_into_agent_memory(self, temp_workspace):
        """_inject_repair_task_summary 将结构化摘要写入 Agent working memory。"""
        from agent_runtime.config import AgentConfig
        from agent_runtime.providers.clients import FakeModelClient
        from agent_runtime.runtime import Agent
        from agent_runtime.workspace import WorkspaceContext

        ws = WorkspaceContext.build(str(temp_workspace))
        agent = Agent(
            config=AgentConfig(),
            model_client=FakeModelClient(outputs=["<final>ok</final>"]),
            workspace=ws,
            cwd=str(temp_workspace),
        )
        state = RepairState(issue_input="fix bug")
        state.repair_plan = RepairPlan(
            issue_type="type_error",
            reasoning="app.py:42",
            suspect_files=["app.py", "test_app.py"],
        )

        Orchestrator._inject_repair_task_summary(agent, state)

        mem = agent.session.get("memory", {}).get("working", {})
        summary = mem.get("task_summary", "")
        assert "type_error" in summary
        assert "app.py" in summary

    def test_no_plan_skips_injection(self, temp_workspace):
        """repair_plan=None 时静默跳过。"""
        from agent_runtime.config import AgentConfig
        from agent_runtime.providers.clients import FakeModelClient
        from agent_runtime.runtime import Agent
        from agent_runtime.workspace import WorkspaceContext

        ws = WorkspaceContext.build(str(temp_workspace))
        agent = Agent(
            config=AgentConfig(),
            model_client=FakeModelClient(outputs=["<final>ok</final>"]),
            workspace=ws,
            cwd=str(temp_workspace),
        )
        state = RepairState(issue_input="fix bug")
        state.repair_plan = None

        # 不应抛异常
        Orchestrator._inject_repair_task_summary(agent, state)

    def test_inject_includes_issue_type(self, temp_workspace):
        from agent_runtime.config import AgentConfig
        from agent_runtime.providers.clients import FakeModelClient
        from agent_runtime.runtime import Agent
        from agent_runtime.workspace import WorkspaceContext

        ws = WorkspaceContext.build(str(temp_workspace))
        agent = Agent(
            config=AgentConfig(),
            model_client=FakeModelClient(outputs=["<final>ok</final>"]),
            workspace=ws,
            cwd=str(temp_workspace),
        )
        state = RepairState(issue_input="fix bug")
        state.repair_plan = RepairPlan(
            issue_type="test_failure",
            reasoning="test_add failed",
            suspect_files=["test_app.py"],
        )

        Orchestrator._inject_repair_task_summary(agent, state)
        summary = agent.session["memory"]["working"]["task_summary"]
        assert "[test_failure]" in summary

    def test_inject_truncates_long_reasoning(self, temp_workspace):
        from agent_runtime.config import AgentConfig
        from agent_runtime.providers.clients import FakeModelClient
        from agent_runtime.runtime import Agent
        from agent_runtime.workspace import WorkspaceContext

        ws = WorkspaceContext.build(str(temp_workspace))
        agent = Agent(
            config=AgentConfig(),
            model_client=FakeModelClient(outputs=["<final>ok</final>"]),
            workspace=ws,
            cwd=str(temp_workspace),
        )
        state = RepairState(issue_input="fix bug")
        state.repair_plan = RepairPlan(
            issue_type="logic_error",
            reasoning="x" * 300,  # 超 200 字符
        )

        Orchestrator._inject_repair_task_summary(agent, state)
        summary = agent.session["memory"]["working"]["task_summary"]
        assert len(summary) < 350  # 被截断


# ---------------------------------------------------------------------------
# _build_feedback 反馈环增强（V1.4-Bonus8a）
# ---------------------------------------------------------------------------


class TestBuildFeedback:
    def test_basic_failure_feedback(self):
        """基本验证失败反馈含指导信息。"""
        orch = Orchestrator.__new__(Orchestrator)
        result = VerificationResult(
            all_passed=False,
            failure_logs=["test_add FAILED: assert 3 == 5"],
        )
        feedback = orch._build_feedback(result)
        assert "补丁验证失败" in feedback
        assert "失败测试" in feedback
        assert "test_add" in feedback
        assert "[指导]" in feedback

    def test_includes_previous_patches(self):
        """包含上轮失败的 patch diff。"""
        orch = Orchestrator.__new__(Orchestrator)
        result = VerificationResult(all_passed=False)
        state = RepairState(issue_input="fix")
        state.candidate_patches = [
            CandidatePatch(
                file_path="app.py", diff="--- a/app.py\n+++ b/app.py\n@@ -1 +1 @@\n-x = 1\n+x = 2"
            ),
        ]
        feedback = orch._build_feedback(result, state=state)
        assert "上轮改动" in feedback
        assert "app.py" in feedback
        assert "x = 2" in feedback

    def test_includes_regression_hint(self):
        """regression 检测时包含回滚提示。"""
        orch = Orchestrator.__new__(Orchestrator)
        result = VerificationResult(all_passed=False)
        state = RepairState(issue_input="fix")
        state.node_timings["introduced_regression"] = True
        feedback = orch._build_feedback(result, state=state)
        assert "回滚提示" in feedback
        assert "回归" in feedback

    def test_includes_build_log(self):
        orch = Orchestrator.__new__(Orchestrator)
        result = VerificationResult(
            all_passed=False,
            build_log="ERROR: pip install failed",
        )
        feedback = orch._build_feedback(result)
        assert "构建日志" in feedback
        assert "pip install" in feedback

    def test_includes_retry_count_guidance(self):
        orch = Orchestrator.__new__(Orchestrator)
        result = VerificationResult(all_passed=False)
        state = RepairState(issue_input="fix")
        state.retry_count = 2
        feedback = orch._build_feedback(result, state=state)
        assert "3 次" in feedback  # retry_count=2 → "已尝试 3 次"

    def test_feedback_extracts_failure_targets_for_retry(self):
        orch = Orchestrator.__new__(Orchestrator)
        result = VerificationResult(
            all_passed=False,
            failure_logs=[
                "FAILED test_018.py::test_report_bob - ValueError: invalid literal",
                'File "values.py", line 3, in get_score',
                "E assert 'N/A points' == '0 points'",
            ],
        )
        feedback = orch._build_feedback(result, state=RepairState(issue_input="fix"))

        assert "失败定位" in feedback
        assert "test_018.py::test_report_bob" in feedback
        assert "values.py" in feedback

    def test_feedback_guides_invalid_literal_value_error(self):
        orch = Orchestrator.__new__(Orchestrator)
        result = VerificationResult(
            all_passed=False,
            failure_logs=["ValueError: invalid literal for int() with base 10: 'N/A'"],
        )
        feedback = orch._build_feedback(result, state=RepairState(issue_input="fix"))

        assert "非数字字符串" in feedback
        assert "N/A" in feedback
        assert "dict.get" in feedback

    def test_feedback_uses_issue_for_value_error_constraints(self):
        orch = Orchestrator.__new__(Orchestrator)
        result = VerificationResult(all_passed=False, failure_logs=["assert failed"])
        state = RepairState(
            issue_input="ValueError: invalid literal for int() with base 10: 'N/A'"
        )
        feedback = orch._build_feedback(result, state=state)

        assert "非数字字符串" in feedback
        assert "raw.isdigit" in feedback

    def test_feedback_warns_on_repeated_patch_fingerprint(self):
        orch = Orchestrator.__new__(Orchestrator)
        result = VerificationResult(all_passed=False, failure_logs=["same failure"])
        state = RepairState(issue_input="fix", retry_count=1)
        patch = CandidatePatch(
            file_path="values.py",
            diff="-return int(data[name])\n+return int(data.get(name, '0') or 0)",
        )
        state.candidate_patches = [patch]

        orch._build_feedback(result, state=state)
        feedback = orch._build_feedback(result, state=state)

        assert "重复补丁" in feedback
        assert "不要重复生成上轮相同 diff" in feedback

    def test_sections_ordered_by_priority(self):
        """Sections 按 priority 排序。"""
        orch = Orchestrator.__new__(Orchestrator)
        result = VerificationResult(
            all_passed=False,
            failure_logs=["test failed"],
            build_log="build error",
        )
        state = RepairState(issue_input="fix")
        state.node_timings["introduced_regression"] = True
        state.candidate_patches = [
            CandidatePatch(file_path="x.py", diff="..."),
        ]

        feedback = orch._build_feedback(result, state=state)
        # 回滚提示 (priority=10) 应在 上轮改动 (20) 之前
        assert feedback.index("回滚提示") < feedback.index("上轮改动")
        # 上轮改动 (20) 应在 失败测试 (30) 之前
        assert feedback.index("上轮改动") < feedback.index("失败测试")

    def test_no_state_handled_gracefully(self):
        """state=None 时不抛异常。"""
        orch = Orchestrator.__new__(Orchestrator)
        result = VerificationResult(all_passed=False)
        feedback = orch._build_feedback(result, state=None)
        assert "补丁验证失败" in feedback

    def test_empty_result(self):
        orch = Orchestrator.__new__(Orchestrator)
        result = VerificationResult(all_passed=False)
        feedback = orch._build_feedback(result)
        assert "[指导]" in feedback  # 至少包含指导


# ---------------------------------------------------------------------------
# Retriever 降级规则检索（V1.4-Bonus12b）
# ---------------------------------------------------------------------------


class TestRetrieverDegrade:
    def test_invalid_llm_output_degrades_after_one_attempt(self, temp_workspace):
        """Retriever 输出不可解析时只尝试一次 LLM，然后降级到规则检索。"""
        (temp_workspace / "app.py").write_text("def add(a, b):\n    return a + b\n")
        (temp_workspace / "tests").mkdir()
        (temp_workspace / "tests" / "test_app.py").write_text(
            "from app import add\n\n\ndef test_add():\n    assert add(1, 2) == 3\n",
            encoding="utf-8",
        )
        ws = WorkspaceContext.build(str(temp_workspace))
        loc_client = FakeModelClient(
            [
                '<final>[{"file_path":"app.py","start_line":1,"end_line":2,'
                '"function_name":"add","reason":"stack","confidence":0.9}]</final>'
            ]
        )
        ret_client = FakeModelClient(
            [
                "<final>I found the test but forgot JSON</final>",
                '<final>{"related_tests":["test_app.py::test_add"]}</final>',
            ]
        )
        orch = Orchestrator(
            create_localizer(loc_client, ws),
            create_retriever(ret_client, ws),
            None,
        )
        state = RepairState(
            issue_input="TypeError at app.py:2 in add",
            repair_plan=RepairPlan(issue_type="type_error", suspect_files=["app.py"]),
        )

        suspects, context, _, _ = orch._run_localize_and_retrieve(state)

        assert [s.file_path for s in suspects] == ["app.py"]
        assert len(ret_client.prompts) == 1
        assert state.node_timings["retrieval_path"] == "llm→degrade"
        assert state.node_timings["retriever_degrade_reason"] == "invalid_json"
        assert any("test_app.py" in test for test in context.related_tests)

    def test_rule_retrieve_returns_context(self, temp_workspace):
        """_rule_retrieve 在无 LLM 时也能返回 RetrievedContext。"""
        orch = Orchestrator.__new__(Orchestrator)
        orch._repo_root = str(temp_workspace)
        (temp_workspace / "app.py").write_text("def add(a,b): return a+b\n")
        suspects = [
            SuspectLocation(
                file_path="app.py",
                start_line=1,
                end_line=10,
                function_name="add",
                reason="traceback",
            ),
        ]
        ctx, timing = orch._rule_retrieve(suspects, "TypeError in add")
        assert ctx is not None

    def test_rule_retrieve_has_required_fields(self, temp_workspace):
        """_rule_retrieve 返回的 context 包含 required fields。"""
        orch = Orchestrator.__new__(Orchestrator)
        orch._repo_root = str(temp_workspace)
        (temp_workspace / "app.py").write_text("def process(x): return x\n")
        suspects = [
            SuspectLocation(
                file_path="app.py",
                start_line=1,
                end_line=10,
                function_name="process",
                reason="stack",
            ),
        ]
        ctx, timing = orch._rule_retrieve(suspects, "error in process")
        assert hasattr(ctx, "related_tests")
        assert hasattr(ctx, "similar_snippets")
