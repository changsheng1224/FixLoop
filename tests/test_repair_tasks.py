"""L2 repair user message 模板单测。"""

from src.prompts.repair_tasks import (
    build_localizer_variables,
    build_retriever_template_and_variables,
    render_repair_task,
)
from src.state import RepairPlan, SuspectLocation


class TestLocalizerTaskTemplate:
    def test_import_error_hints(self):
        plan = RepairPlan(
            issue_type="import_error",
            suspect_files=["app.py"],
            reasoning="app.py:1",
        )
        text, meta = render_repair_task(
            "localizer", build_localizer_variables(plan, "ImportError")[0]
        )
        assert "定位以下问题" in text
        assert "ImportError" in text
        assert "嫌疑文件: app.py" in text
        assert "import 错误" in text
        assert meta["task_template_source"] == "src/prompts/tasks/localizer.md"

    def test_default_stack_hints(self):
        plan = RepairPlan(issue_type="type_error", reasoning="calc.py:2")
        text, _ = render_repair_task("localizer", build_localizer_variables(plan)[0])
        assert "stack_parse" in text


class TestRetrieverTaskTemplate:
    def test_with_suspects(self):
        suspects = [
            SuspectLocation(file_path="a.py", start_line=1, end_line=2, function_name="f"),
        ]
        name, vars_, _ = build_retriever_template_and_variables(suspects)
        text, _ = render_repair_task(name, vars_)
        assert name == "retriever_suspects"
        assert "a.py:1 f" in text
        assert "RetrievedContext JSON" in text

    def test_fallback(self):
        name, vars_, _ = build_retriever_template_and_variables([])
        text, _ = render_repair_task(name, vars_)
        assert name == "retriever_fallback"
        assert "搜索与该 Issue 相关的代码上下文" in text


class TestOrchestratorTemplateParity:
    def test_patcher_prompt_unchanged_semantics(self, temp_workspace):
        from src.orchestrator import Orchestrator
        from src.state import RepairPlan

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
        assert "直接输出 CandidatePatch JSON" in prompt
