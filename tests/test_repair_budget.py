"""RepairBudgetContext 共享 TokenBudget 库单测（V1.4-Bonus3c）。"""

from __future__ import annotations

import tempfile

from agent_runtime.config import AgentConfig
from agent_runtime.context_manager import ContextManager, TokenBudget, TOTAL_BUDGET
from agent_runtime.providers.clients import FakeModelClient
from agent_runtime.repair_budget import RepairBudgetContext, _DEFAULT_ALLOCATIONS
from agent_runtime.runtime import Agent
from agent_runtime.workspace import WorkspaceContext


# ---------------------------------------------------------------------------
# RepairBudgetContext 基本功能
# ---------------------------------------------------------------------------


class TestRepairBudgetContextBasic:
    def test_create_with_defaults(self):
        ctx = RepairBudgetContext.create()
        assert isinstance(ctx.master, TokenBudget)
        assert ctx.master.total_limit == 100_000
        assert ctx.allocations == _DEFAULT_ALLOCATIONS

    def test_create_custom_allocations(self):
        ctx = RepairBudgetContext.create(
            allocations={"localizer": 1000, "patcher": 5000},
        )
        assert ctx.allocations["localizer"] == 1000
        assert ctx.allocations["patcher"] == 5000
        # 未指定的使用默认值
        assert ctx.allocations["retriever"] == _DEFAULT_ALLOCATIONS["retriever"]

    def test_sub_budget_returns_role_specific_limit(self):
        ctx = RepairBudgetContext.create()
        loc = ctx.sub_budget("localizer")
        ret = ctx.sub_budget("retriever")
        pat = ctx.sub_budget("patcher")

        assert loc.total_limit == 2000
        assert ret.total_limit == 3000
        assert pat.total_limit == 4000
        # 共享同一个 tokenizer backend
        assert loc.backend == ctx.master.backend

    def test_sub_budget_unknown_role_uses_default(self):
        ctx = RepairBudgetContext.create()
        budget = ctx.sub_budget("unknown_role")
        assert budget.total_limit == TOTAL_BUDGET

    def test_sub_budgets_are_independent(self):
        """不同角色的子预算互不影响。"""
        ctx = RepairBudgetContext.create()
        loc = ctx.sub_budget("localizer")
        ret = ctx.sub_budget("retriever")
        assert loc.total_limit == 2000
        assert ret.total_limit == 3000
        # 修改一个不影响另一个
        loc.total_limit = 999
        assert ret.total_limit == 3000


# ---------------------------------------------------------------------------
# 消耗追踪
# ---------------------------------------------------------------------------


class TestUsageTracking:
    def test_track_single_agent(self):
        ctx = RepairBudgetContext.create()
        ctx.track_usage("localizer", 500, 200)
        assert ctx.total_input == 500
        assert ctx.total_output == 200

    def test_track_multiple_agents(self):
        ctx = RepairBudgetContext.create()
        ctx.track_usage("localizer", 500, 200)
        ctx.track_usage("retriever", 300, 150)
        ctx.track_usage("patcher", 1000, 500)
        assert ctx.total_input == 1800
        assert ctx.total_output == 850

    def test_track_accumulates(self):
        ctx = RepairBudgetContext.create()
        ctx.track_usage("patcher", 100, 50)
        ctx.track_usage("patcher", 200, 100)
        summary = ctx.usage_summary
        assert summary["patcher"]["input_tokens"] == 300
        assert summary["patcher"]["output_tokens"] == 150
        assert summary["patcher"]["calls"] == 2

    def test_usage_summary_is_copy(self):
        ctx = RepairBudgetContext.create()
        ctx.track_usage("localizer", 100, 50)
        summary = ctx.usage_summary
        summary["localizer"]["input_tokens"] = 999
        # 原数据不变
        assert ctx.usage_summary["localizer"]["input_tokens"] == 100

    def test_empty_usage(self):
        ctx = RepairBudgetContext.create()
        assert ctx.total_input == 0
        assert ctx.total_output == 0
        assert ctx.usage_summary == {}


# ---------------------------------------------------------------------------
# ContextManager 集成
# ---------------------------------------------------------------------------


class TestContextManagerWithBudget:
    def test_accepts_external_budget(self):
        with tempfile.TemporaryDirectory() as tmp:
            ws = WorkspaceContext.build(tmp)
            agent = Agent(
                config=AgentConfig(),
                model_client=FakeModelClient(outputs=["<final>ok</final>"]),
                workspace=ws,
                cwd=tmp,
            )
            budget = TokenBudget(model="gpt-4", provider="openai", total_limit=3000)
            cm = ContextManager(agent, budget=budget)
            assert cm.budget is budget
            assert cm.budget.total_limit == 3000

    def test_uses_agent_budget_when_set(self):
        with tempfile.TemporaryDirectory() as tmp:
            ws = WorkspaceContext.build(tmp)
            agent = Agent(
                config=AgentConfig(),
                model_client=FakeModelClient(outputs=["<final>ok</final>"]),
                workspace=ws,
                cwd=tmp,
            )
            budget = TokenBudget(model="gpt-4", provider="openai", total_limit=5000)
            agent._budget = budget
            cm = ContextManager(agent)
            assert cm.budget is budget
            assert cm.budget.total_limit == 5000

    def test_explicit_budget_overrides_agent_budget(self):
        with tempfile.TemporaryDirectory() as tmp:
            ws = WorkspaceContext.build(tmp)
            agent = Agent(
                config=AgentConfig(),
                model_client=FakeModelClient(outputs=["<final>ok</final>"]),
                workspace=ws,
                cwd=tmp,
            )
            agent._budget = TokenBudget(total_limit=5000)
            explicit = TokenBudget(total_limit=1000)
            cm = ContextManager(agent, budget=explicit)
            assert cm.budget is explicit
            assert cm.budget.total_limit == 1000

    def test_no_budget_creates_default(self):
        with tempfile.TemporaryDirectory() as tmp:
            ws = WorkspaceContext.build(tmp)
            agent = Agent(
                config=AgentConfig(),
                model_client=FakeModelClient(outputs=["<final>ok</final>"]),
                workspace=ws,
                cwd=tmp,
            )
            cm = ContextManager(agent)
            assert cm.budget is not None
            assert cm.budget.total_limit == agent.config.prompt_budget


# ---------------------------------------------------------------------------
# RepairBudgetContext → Agent → ContextManager 端到端
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# 分 Agent 预算表 — 统一来源验证
# ---------------------------------------------------------------------------


class TestAgentBudgetDefaults:
    """_AGENT_DEFAULTS.prompt_budget 与 _DEFAULT_ALLOCATIONS 一致。"""

    def test_factory_defaults_match_allocations(self):
        from src.agents.factory import _AGENT_DEFAULTS as FACTORY_DEFAULTS

        for role, alloc in _DEFAULT_ALLOCATIONS.items():
            if role in FACTORY_DEFAULTS:
                assert FACTORY_DEFAULTS[role]["prompt_budget"] == alloc, (
                    f"{role}: factory prompt_budget={FACTORY_DEFAULTS[role]['prompt_budget']} "
                    f"!= allocation={alloc}"
                )

    def test_all_roles_have_prompt_budget(self):
        from src.agents.factory import _AGENT_DEFAULTS as FACTORY_DEFAULTS

        for role, defaults in FACTORY_DEFAULTS.items():
            assert "prompt_budget" in defaults, (
                f"{role} 缺少 prompt_budget 字段"
            )
            assert defaults["prompt_budget"] > 0, (
                f"{role} prompt_budget 应为正数"
            )

    def test_localizer_budget_is_2k(self):
        from src.agents.factory import _AGENT_DEFAULTS as FACTORY_DEFAULTS
        assert FACTORY_DEFAULTS["localizer"]["prompt_budget"] == 2000

    def test_patcher_budget_is_4k(self):
        from src.agents.factory import _AGENT_DEFAULTS as FACTORY_DEFAULTS
        assert FACTORY_DEFAULTS["patcher"]["prompt_budget"] == 4000

    def test_create_repair_agent_uses_role_budget(self, temp_workspace):
        """create_repair_agent 产出 Agent 的 prompt_budget 正确。"""
        from src.agents.factory import create_repair_agent
        from agent_runtime.workspace import WorkspaceContext

        ws = WorkspaceContext.build(str(temp_workspace))
        client = FakeModelClient(outputs=["<final>ok</final>"])

        for role, expected in [("localizer", 2000), ("retriever", 3000),
                                ("patcher", 4000), ("verifier", 1000)]:
            agent = create_repair_agent(role, client, ws, cwd=str(temp_workspace),
                                        approval="auto", dry_run=True)
            assert agent.config.prompt_budget == expected, (
                f"{role}: expected prompt_budget={expected}, got={agent.config.prompt_budget}"
            )


class TestBudgetE2E:
    def test_repair_budget_flows_to_context_manager(self):
        """RepairBudgetContext.sub_budget → Agent._budget → ContextManager。"""
        budget_ctx = RepairBudgetContext.create()
        sub = budget_ctx.sub_budget("patcher")

        with tempfile.TemporaryDirectory() as tmp:
            ws = WorkspaceContext.build(tmp)
            agent = Agent(
                config=AgentConfig(provider="deepseek", prompt_budget=100000),
                model_client=FakeModelClient(outputs=["<final>ok</final>"]),
                workspace=ws,
                cwd=tmp,
            )
            agent._budget = sub
            cm = ContextManager(agent)
            # ContextManager 使用注入的子预算
            assert cm.budget is sub
            assert cm.budget.total_limit == 4000  # patcher 分配值

    def test_build_with_repair_budget(self):
        """使用 RepairBudgetContext 子预算构建 prompt。"""
        budget_ctx = RepairBudgetContext.create()
        sub = budget_ctx.sub_budget("localizer")  # 2000 tokens

        with tempfile.TemporaryDirectory() as tmp:
            ws = WorkspaceContext.build(tmp)
            agent = Agent(
                config=AgentConfig(provider="deepseek"),
                model_client=FakeModelClient(outputs=["<final>ok</final>"]),
                workspace=ws,
                cwd=tmp,
            )
            agent._budget = sub
            cm = ContextManager(agent)
            prompt, meta = cm.build("find the bug")
            # prompt 成功构建
            assert len(prompt) > 0
            assert meta["budget"] == 2000

    def test_wire_orchestrator_injects_budget(self, tmp_path):
        """wire_orchestrator 创建 RepairBudgetContext 并注入各 Agent。"""
        from src.repair_factory import wire_orchestrator
        from src.orchestrator import Orchestrator

        orch = wire_orchestrator(
            FakeModelClient(outputs=["<final>ok</final>"]),
            str(tmp_path),
            skip_verify=True,
            dry_run=True,
        )
        assert isinstance(orch, Orchestrator)
        # 各 Agent 应持有子预算
        assert orch.localizer._budget is not None
        assert orch.retriever._budget is not None
        assert orch.patcher._budget is not None
        # 子预算的 total_limit 应与分配表一致
        assert orch.localizer._budget.total_limit == 2000
        assert orch.retriever._budget.total_limit == 3000
        assert orch.patcher._budget.total_limit == 4000
        # Orchestrator 持有 budget_ctx
        assert orch._budget_ctx is not None
        assert orch._budget_ctx.total_input == 0  # 未调用 ask()
