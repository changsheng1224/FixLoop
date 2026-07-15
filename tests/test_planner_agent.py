"""Planner Agent 单测：LLM 单次 JSON → RepairPlan + 回落规则。"""


class TestPlannerPrompt:
    def test_prompt_contains_json_schema(self):
        from src.agents.planner import PLANNER_PROMPT

        assert "issue_type" in PLANNER_PROMPT
        assert "suspect_files" in PLANNER_PROMPT
        assert "subtasks" in PLANNER_PROMPT

    def test_prompt_forbids_tool_calls(self):
        from src.agents.planner import PLANNER_PROMPT

        assert "不要调用任何工具" in PLANNER_PROMPT


class TestCreatePlanner:
    def test_planner_has_minimal_config(self):
        """Planner 配置设置正确。"""
        import tempfile

        from agent_runtime.providers.clients import FakeModelClient
        from agent_runtime.workspace import WorkspaceContext
        from src.agents.planner import create_planner

        with tempfile.TemporaryDirectory() as tmp:
            ws = WorkspaceContext.build(tmp)
            client = FakeModelClient(["{}"])
            agent = create_planner(client, ws, cwd=tmp)
            assert agent.config.max_steps == 1  # 只做单次 complete

    def test_planner_has_no_tools(self):
        import tempfile

        from agent_runtime.providers.clients import FakeModelClient
        from agent_runtime.workspace import WorkspaceContext
        from src.agents.planner import create_planner

        with tempfile.TemporaryDirectory() as tmp:
            ws = WorkspaceContext.build(tmp)
            client = FakeModelClient(["{}"])
            agent = create_planner(client, ws, cwd=tmp)
            assert len(agent.tools) == 0


class TestPlanWithLLM:
    def test_valid_json_returns_dict(self):
        """Planner 返回合法 JSON 时输出 dict。"""
        from unittest.mock import MagicMock

        from src.repair.pipeline import RepairPipelineMixin

        mixin = RepairPipelineMixin()
        mixin._light_client = MagicMock()
        mixin._light_client.complete.return_value = (
            '{"issue_type":"type_error","reasoning":"int+str","suspect_files":["calc.py"]}'
        )
        mixin._active_repair_ctx = lambda: MagicMock()

        result = mixin._plan_with_llm("TypeError at calc.py:42")
        assert result is not None
        assert result["issue_type"] == "type_error"
        assert "calc.py" in result["suspect_files"]

    def test_invalid_json_returns_none(self):
        """Planner 返回非法 JSON 时返回 None。"""
        from unittest.mock import MagicMock

        from src.repair.pipeline import RepairPipelineMixin

        mixin = RepairPipelineMixin()
        mixin._light_client = MagicMock()
        mixin._light_client.complete.return_value = "not json at all"
        mixin._active_repair_ctx = lambda: MagicMock()

        result = mixin._plan_with_llm("issue")
        assert result is None

    def test_no_client_returns_none(self):
        """无 light_client 时返回 None。"""
        from src.repair.pipeline import RepairPipelineMixin

        mixin = RepairPipelineMixin()
        mixin._light_client = None
        result = mixin._plan_with_llm("issue")
        assert result is None


class TestApplyPlannerResult:
    def test_overrides_issue_type(self):
        from src.repair.pipeline import RepairPipelineMixin
        from src.state import RepairPlan

        plan = RepairPlan(issue_type="unknown")
        mixin = RepairPipelineMixin()
        mixin._apply_planner_result(
            {"issue_type": "composite", "suspect_files": ["a.py", "b.py"]},
            plan,
        )
        assert plan.issue_type == "composite"
        assert plan.suspect_files == ["a.py", "b.py"]
        assert plan.intent_parser == "llm"

    def test_sets_subtasks_from_planner(self):
        from src.repair.pipeline import RepairPipelineMixin
        from src.state import RepairPlan

        plan = RepairPlan(issue_type="composite")
        mixin = RepairPipelineMixin()
        mixin._apply_planner_result(
            {
                "issue_type": "composite",
                "subtasks": [
                    {"id": "fix_a", "goal": "fix a.py", "suspect_files": ["a.py"]},
                    {"id": "fix_b", "goal": "fix b.py", "suspect_files": ["b.py"]},
                ],
            },
            plan,
        )
        assert len(plan.subtasks) == 2
        assert plan.subtasks[0].id == "fix_a"

    def test_partial_result_does_not_clear_existing(self):
        from src.repair.pipeline import RepairPipelineMixin
        from src.state import RepairPlan

        plan = RepairPlan(
            issue_type="type_error",
            suspect_files=["calc.py"],
            reasoning="original reason",
        )
        mixin = RepairPipelineMixin()
        # Planner 只返回 issue_type，不改其他字段
        mixin._apply_planner_result(
            {"issue_type": "import_error"},
            plan,
        )
        assert plan.issue_type == "import_error"
        # 其他字段保持原值
        assert plan.suspect_files == ["calc.py"]
