"""Tier pins enforce 单测：钉扎区字段在 L0 裁剪与 fit_repair 后保留。

验证 tier_pins.yaml 定义的 pin_roles / pin_content_markers / orchestrator_pin_fields
在超长 history + 极小 budget 场景下不会被裁剪丢弃。
"""

import pytest

from agent_runtime.config import AgentConfig
from agent_runtime.context_manager import ContextManager, fit_repair_user_prompt
from agent_runtime.providers.clients import FakeModelClient
from agent_runtime.runtime import Agent
from agent_runtime.tier_policy import (
    PIN_CONTENT_MARKERS,
    TierPolicy,
    is_pinned_history_item,
    l0_filter_history,
)
from agent_runtime.workspace import WorkspaceContext

ISSUE_MARKER = "PINNED_ISSUE_CALC_42"
SUSPECT_FILE = "src/calculator.py"


@pytest.fixture
def agent(temp_workspace):
    config = AgentConfig(provider="openai", model="gpt-4", max_steps=1, prompt_budget=800)
    ws = WorkspaceContext.build(str(temp_workspace))
    return Agent(config=config, model_client=FakeModelClient([]), workspace=ws)


# ---------------------------------------------------------------------------
# fit_repair_user_prompt 钉扎 — user 文本永不裁剪
# ---------------------------------------------------------------------------


class TestFitRepairPins:
    """fit_repair_user_prompt：user 文本全文保留。"""

    def test_issue_marker_survives_tiny_budget(self, agent):
        """极低 budget 下 issue 标记仍然完整保留在 fitted 文本中。"""
        issue = f"{ISSUE_MARKER}\nError: Traceback (most recent call last):\n" + "trace " * 300
        fitted, meta = fit_repair_user_prompt(agent, issue, "system " * 500)
        assert ISSUE_MARKER in fitted
        assert meta.get("request_preserved") is True

    def test_suspect_file_path_survives_in_long_user_text(self, agent):
        """超长 user 文本中 suspect 文件路径不被截断。"""
        suspect_block = (
            f"suspect: {SUSPECT_FILE}:42 (TypeError: unsupported operand)\n"
            f"suspect: src/utils/helpers.py:15 (ImportError)\n"
        )
        user_text = suspect_block + "padding " * 400
        fitted, meta = fit_repair_user_prompt(agent, user_text, "system " * 500)
        assert SUSPECT_FILE in fitted
        assert "src/utils/helpers.py" in fitted
        assert meta.get("request_preserved") is True

    def test_user_text_not_truncated_even_when_exceeds_budget(self, agent):
        """user 文本超过 budget 时仍然全文保留（task_budget_overflow）。"""
        long_issue = f"{ISSUE_MARKER} " + ("overflow " * 2000)
        fitted, meta = fit_repair_user_prompt(agent, long_issue, "sys")
        assert fitted == long_issue
        assert meta.get("task_budget_overflow") is True
        assert meta["total_tokens"] > meta["budget"]

    def test_orch_pin_field_issue_survives(self, agent):
        """orchestrator_pin_fields 中 'issue' 在超长文本中保留。"""
        user_text = f"issue: {ISSUE_MARKER} TypeError at line 42\n" + "filler " * 400
        fitted, _ = fit_repair_user_prompt(agent, user_text, "system " * 500)
        assert ISSUE_MARKER in fitted

    def test_orch_pin_field_stack_survives(self, agent):
        """orchestrator_pin_fields 中 'stack' 在超长文本中保留。"""
        user_text = (
            f"stack: File \"{SUSPECT_FILE}\", line 42, in add\n"
            "    return a + b\n"
            "TypeError: unsupported operand type(s) for +: 'int' and 'str'\n"
            + "filler " * 400
        )
        fitted, _ = fit_repair_user_prompt(agent, user_text, "system " * 500)
        assert SUSPECT_FILE in fitted


# ---------------------------------------------------------------------------
# L0 filter 钉扎 — is_pinned_history_item
# ---------------------------------------------------------------------------


class TestL0FilterPins:
    """L0 过滤：钉扎条目不被丢弃。"""

    def test_user_role_is_pinned(self):
        """pin_roles 中的 'user' 角色始终钉扎。"""
        policy = TierPolicy(pin_roles=frozenset({"user"}))
        item = {"role": "user", "content": "fix the bug"}
        assert is_pinned_history_item(item, policy) is True

    def test_traceback_content_is_pinned(self):
        """含 'Traceback' 关键词的 content 被钉扎。"""
        policy = TierPolicy(pin_roles=frozenset())
        item = {"role": "tool", "content": "Error: Traceback (most recent call last):\n  ..."}
        assert is_pinned_history_item(item, policy) is True

    def test_earlier_summary_content_is_pinned(self):
        """含 '[Earlier summary]' 的 content 被钉扎。"""
        policy = TierPolicy(pin_roles=frozenset())
        assert "[Earlier summary]" in PIN_CONTENT_MARKERS
        item = {"role": "system", "content": "[Earlier summary]: Fixed import error"}
        assert is_pinned_history_item(item, policy) is True

    def test_error_keyword_content_is_pinned(self):
        """含 'FAILED' / 'error:' 关键词的 content 被钉扎。"""
        policy = TierPolicy(pin_roles=frozenset())
        assert is_pinned_history_item(
            {"role": "tool", "content": "FAILED: test_add - AssertionError"}, policy
        ) is True
        assert is_pinned_history_item(
            {"role": "tool", "content": "error: module not found"}, policy
        ) is True

    def test_orch_pin_field_issue_is_pinned(self):
        """content 开头含 orchestator_pin_fields 时钉扎。"""
        policy = TierPolicy(pin_roles=frozenset())
        item = {"role": "tool", "content": "issue: TypeError at calculator.py:42"}
        assert is_pinned_history_item(item, policy) is True

    def test_orch_pin_field_request_is_pinned(self):
        """content 含 'request: ...' 时钉扎。"""
        policy = TierPolicy(pin_roles=frozenset())
        item = {"role": "tool", "content": "\nrequest: fix import error in utils.py"}
        assert is_pinned_history_item(item, policy) is True

    def test_ordinary_tool_result_not_pinned(self):
        """普通 tool 输出（无 pin 标记）不被钉扎。"""
        policy = TierPolicy(pin_roles=frozenset())
        item = {"role": "tool", "content": "test_add PASSED\ntest_sub PASSED"}
        assert is_pinned_history_item(item, policy) is False

    def test_pinned_item_bypasses_rejection(self):
        """含 Traceback 的 tool 结果仍会被 rejection 检查拦截（安全优先）。

        is_rejected_tool_content 在 is_pinned_history_item 之前执行，
        确保即使内容含 pin 标记，已知的 rejection 模式也不通过。
        """
        policy = TierPolicy(pin_roles=frozenset({"user"}))
        # 两条都匹配 rejection pattern → 都被 drop
        history = [
            {"role": "tool", "content": "Error: Traceback ... 重复调用检测 triggered"},
            {"role": "tool", "content": "Error: 不可用。请检查配置。"},
        ]
        filtered, stats = l0_filter_history(history, policy)
        # 两条都是 rejected tool content → 全部 drop（rejection 优先于 pin）
        assert stats["dropped"] == 2
        assert len(filtered) == 0

    def test_pinned_non_tool_survives_filter(self):
        """非 tool 角色的 pin 条目不受 tool rejection 影响，正确保留。"""
        policy = TierPolicy(pin_roles=frozenset({"user"}))
        history = [
            {"role": "user", "content": "fix the bug"},      # pin role → 保留
            {"role": "system", "content": "Error: Traceback ... 分析"},  # pin content → 保留
            {"role": "system", "content": "plain system message"},  # 无 pin → 保留
        ]
        filtered, stats = l0_filter_history(history, policy)
        assert stats["dropped"] == 0
        assert len(filtered) == 3

    def test_user_role_bypasses_rejection(self):
        """user 角色钉扎，即使 content 匹配 rejection 也不丢弃。"""
        policy = TierPolicy(pin_roles=frozenset({"user"}))
        history = [
            {"role": "user", "content": "Error: 参数校验失败，请重试"},
        ]
        filtered, stats = l0_filter_history(history, policy)
        assert stats["dropped"] == 0
        assert len(filtered) == 1


# ---------------------------------------------------------------------------
# ContextManager 集成 — 钉扎在完整 build 中生效
# ---------------------------------------------------------------------------


class TestTierPinsContextManagerIntegration:
    """ContextManager.build() 联合 tier_pins enforce。"""

    def test_full_build_preserves_issue_with_long_history(self, agent):
        """超长 history + 极小 budget 下 issue 标记仍在 prompt 中。"""
        # 注入大量 history
        for i in range(40):
            agent.record({"role": "user", "content": f"question {i}"})
            agent.record({"role": "tool", "content": f"result {i}: " + "x" * 200})

        issue = f"{ISSUE_MARKER} TypeError at {SUSPECT_FILE}:42\n" + "detail " * 100
        cm = ContextManager(agent, total_budget=600)
        prompt, meta = cm.build(issue)

        assert ISSUE_MARKER in prompt, f"expected {ISSUE_MARKER} in prompt"
        assert meta.get("request_preserved") is True

    def test_state_section_has_suspect_in_prompt(self, agent):
        """plan_todos 中的 suspect 文件路径出现在 prompt state 段。"""
        agent.session["plan_todos"] = [
            {"id": "1", "content": f"定位 {SUSPECT_FILE}:42 检查类型转换", "status": "done"},
            {"id": "2", "content": "检索调用方并搜索类似修复", "status": "in_progress"},
        ]
        agent.session.setdefault("memory", {}).setdefault("working", {})[
            "task_summary"
        ] = f"修复 {SUSPECT_FILE} TypeError"

        cm = ContextManager(agent, total_budget=500)
        prompt, meta = cm.build("fix it")
        # state 段在 prompt 中
        assert SUSPECT_FILE in prompt, f"{SUSPECT_FILE} should be in prompt"
        assert "定位" in prompt
        assert meta.get("request_preserved") is True
