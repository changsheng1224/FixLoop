"""StepGuard 步进健康监控单测（V1.4-Bonus2：目标漂移/stall终止）。"""

from __future__ import annotations

import pytest

from agent_runtime.step_guard import (
    DEFAULT_DRIFT_TERMINATE,
    DEFAULT_DRIFT_WARN,
    DEFAULT_STALL_THRESHOLD,
    StepContext,
    StepGuard,
    StepVerdict,
    _extract_filenames,
    _tool_target_file,
)
from agent_runtime.stop_reasons import StopReason


# ---------------------------------------------------------------------------
# 工具函数
# ---------------------------------------------------------------------------


class TestExtractFilenames:
    def test_single_py_file(self):
        assert _extract_filenames("fix pricing.py") == {"pricing.py"}

    def test_multiple_files(self):
        result = _extract_filenames("see app.py and utils/helpers.py")
        assert result == {"app.py", "helpers.py"}

    def test_path_with_slashes(self):
        result = _extract_filenames("src/eval/cases/case_001/repo/pricing.py")
        assert result == {"pricing.py"}

    def test_no_py_files(self):
        assert _extract_filenames("no python files here") == set()

    def test_empty_text(self):
        assert _extract_filenames("") == set()


class TestToolTargetFile:
    def test_read_file_extracts_path(self):
        assert _tool_target_file("read_file", {"path": "app.py"}) == "app.py"

    def test_write_file_extracts_path(self):
        assert _tool_target_file("write_file", {"path": "src/main.py"}) == "main.py"

    def test_patch_file_extracts_path(self):
        assert _tool_target_file("patch_file", {"path": "utils/helpers.py"}) == "helpers.py"

    def test_non_file_tool_returns_none(self):
        assert _tool_target_file("search", {"pattern": "def"}) is None
        assert _tool_target_file("grep", {"pattern": "TODO"}) is None

    def test_missing_path_returns_none(self):
        assert _tool_target_file("read_file", {}) is None


# ---------------------------------------------------------------------------
# StallDetector（经 StepGuard.evaluate）
# ---------------------------------------------------------------------------


class TestStallDetection:
    def test_resets_on_affected(self):
        guard = StepGuard(stall_threshold=3)
        guard.reset(task_summary="fix app.py")
        # 2 步无变更
        assert guard.evaluate(StepContext(has_affected=False)) is None
        assert guard.evaluate(StepContext(has_affected=False)) is None
        assert guard.stall_count == 2
        # 1 步有变更 → 重置
        assert guard.evaluate(StepContext(has_affected=True)) is None
        assert guard.stall_count == 0

    def test_terminates_at_threshold(self):
        guard = StepGuard(stall_threshold=3)
        guard.reset(task_summary="fix app.py")
        assert guard.evaluate(StepContext(has_affected=False)) is None
        assert guard.evaluate(StepContext(has_affected=False)) is None
        verdict = guard.evaluate(StepContext(has_affected=False))
        assert verdict is not None
        assert verdict.reason == StopReason.STALL.value
        assert "3 步" in verdict.detail
        assert "fix app.py" in verdict.replan_hint

    def test_custom_threshold(self):
        guard = StepGuard(stall_threshold=5)
        guard.reset(task_summary="fix app.py")
        for _ in range(4):
            assert guard.evaluate(StepContext(has_affected=False)) is None
        verdict = guard.evaluate(StepContext(has_affected=False))
        assert verdict is not None
        assert verdict.reason == StopReason.STALL.value

    def test_reset_clears_count(self):
        guard = StepGuard(stall_threshold=3)
        guard.reset(task_summary="fix app.py")
        guard.evaluate(StepContext(has_affected=False))
        guard.evaluate(StepContext(has_affected=False))
        assert guard.stall_count == 2
        # reset 后计数归零
        guard.reset(task_summary="new task")
        assert guard.stall_count == 0

    def test_no_task_summary_uses_placeholder(self):
        guard = StepGuard(stall_threshold=2)
        guard.reset(task_summary="")
        guard.evaluate(StepContext(has_affected=False))
        verdict = guard.evaluate(StepContext(has_affected=False))
        assert verdict is not None
        assert "未知任务" in verdict.replan_hint


# ---------------------------------------------------------------------------
# DriftDetector（经 StepGuard.evaluate）
# ---------------------------------------------------------------------------


class TestDriftDetection:
    def test_related_file_resets_drift(self):
        guard = StepGuard()
        guard.reset(task_summary="fix pricing.py", suspect_files={"pricing.py"})
        # 操作 pricing.py → 不漂移
        assert guard.evaluate(
            StepContext(tool_name="read_file", tool_args={"path": "pricing.py"})
        ) is None
        assert guard.drift_count == 0

    def test_unrelated_file_increments_drift(self):
        guard = StepGuard()
        guard.reset(task_summary="fix pricing.py", suspect_files={"pricing.py"})
        # 操作无关文件 → 漂移计数
        guard.evaluate(
            StepContext(tool_name="read_file", tool_args={"path": "unrelated.py"})
        )
        assert guard.drift_count == 1

    def test_drift_warning_then_terminate(self):
        """M=2 预警，M=3 终止（has_affected=True 避免 stall 抢断）。"""
        guard = StepGuard(stall_threshold=10, drift_warn=2, drift_terminate=3)
        guard.reset(task_summary="fix pricing.py", suspect_files={"pricing.py"})

        # Step 1: 无关文件（has_affected=True 避免计入 stall）
        v1 = guard.evaluate(
            StepContext(tool_name="read_file", tool_args={"path": "other.py"}, has_affected=True)
        )
        assert v1 is None  # 仅 1 步，无动作

        # Step 2: 预警
        v2 = guard.evaluate(
            StepContext(tool_name="read_file", tool_args={"path": "other.py"}, has_affected=True)
        )
        assert v2 is not None
        assert v2.reason == ""  # warning，不终止

        # Step 3: 终止
        v3 = guard.evaluate(
            StepContext(tool_name="read_file", tool_args={"path": "other.py"}, has_affected=True)
        )
        assert v3 is not None
        assert v3.reason == StopReason.GOAL_DRIFT.value
        assert "pricing.py" in v3.detail
        assert "other.py" in v3.detail
        assert "pricing.py" in v3.replan_hint

    def test_related_file_after_drift_resets(self):
        guard = StepGuard(stall_threshold=10, drift_terminate=3)
        guard.reset(task_summary="fix pricing.py", suspect_files={"pricing.py"})

        guard.evaluate(
            StepContext(tool_name="read_file", tool_args={"path": "other.py"}, has_affected=True)
        )
        guard.evaluate(
            StepContext(tool_name="read_file", tool_args={"path": "other.py"}, has_affected=True)
        )
        assert guard.drift_count == 2
        # 回到 suspect 文件 → 重置
        guard.evaluate(
            StepContext(tool_name="read_file", tool_args={"path": "pricing.py"}, has_affected=True)
        )
        assert guard.drift_count == 0

    def test_drift_warning_only_once(self):
        """预警仅触发一次，不重复。"""
        guard = StepGuard(drift_warn=1, drift_terminate=10)
        guard.reset(task_summary="fix pricing.py", suspect_files={"pricing.py"})

        v1 = guard.evaluate(
            StepContext(tool_name="read_file", tool_args={"path": "other.py"})
        )
        assert v1 is not None and v1.reason == ""  # warning

        v2 = guard.evaluate(
            StepContext(tool_name="read_file", tool_args={"path": "other.py"})
        )
        assert v2 is None  # 不再重复 warn

    def test_no_suspect_files_skips_drift(self):
        """无 suspect 信息时跳过漂移检测（has_affected=True 避免 stall）。"""
        guard = StepGuard(stall_threshold=10)
        guard.reset(task_summary="")  # 空 → 无可提取文件名

        # 操作任意文件都不触发 drift
        for _ in range(5):
            v = guard.evaluate(
                StepContext(tool_name="read_file", tool_args={"path": "x.py"}, has_affected=True)
            )
            assert v is None

    def test_non_file_tools_ignored_by_drift(self):
        """search/grep/run_shell 不影响 drift 计数（has_affected=True 避免 stall）。"""
        guard = StepGuard(stall_threshold=10, drift_warn=1, drift_terminate=3)
        guard.reset(task_summary="fix pricing.py", suspect_files={"pricing.py"})

        # 多次调用 search 但不操作文件 → 不触发 drift
        for _ in range(5):
            v = guard.evaluate(
                StepContext(tool_name="search", tool_args={"pattern": "TODO"}, has_affected=True)
            )
            assert v is None
        assert guard.drift_count == 0

    def test_suspect_from_task_summary_extraction(self):
        """不传 suspect_files 时从 task_summary 自动提取。"""
        guard = StepGuard()
        guard.reset(task_summary="fix the bug in src/app.py and utils/helpers.py")

        assert "app.py" in guard.suspect_files
        assert "helpers.py" in guard.suspect_files
        # 操作无关文件
        guard.evaluate(
            StepContext(tool_name="read_file", tool_args={"path": "other.py"})
        )
        assert guard.drift_count == 1


# ---------------------------------------------------------------------------
# Stall + Drift 联合
# ---------------------------------------------------------------------------


class TestStallAndDriftTogether:
    def test_stall_takes_priority_over_drift(self):
        """stall 检测优先于 drift。"""
        guard = StepGuard(stall_threshold=2, drift_terminate=3)
        guard.reset(task_summary="fix pricing.py", suspect_files={"pricing.py"})

        # 操作无关文件 + 无 affected
        guard.evaluate(
            StepContext(
                tool_name="read_file",
                tool_args={"path": "other.py"},
                has_affected=False,
            )
        )
        # 还是无关文件 + 无 affected → stall 先触发
        verdict = guard.evaluate(
            StepContext(
                tool_name="read_file",
                tool_args={"path": "other.py"},
                has_affected=False,
            )
        )
        assert verdict is not None
        assert verdict.reason == StopReason.STALL.value

    def test_drift_terminates_when_stall_not_triggered(self):
        """有 affected 但操作无关文件 → drift 终止。"""
        guard = StepGuard(stall_threshold=5, drift_terminate=2)
        guard.reset(task_summary="fix pricing.py", suspect_files={"pricing.py"})

        guard.evaluate(
            StepContext(
                tool_name="patch_file",
                tool_args={"path": "other.py"},
                has_affected=True,  # 有变更 → stall 不计数
            )
        )
        verdict = guard.evaluate(
            StepContext(
                tool_name="patch_file",
                tool_args={"path": "other.py"},
                has_affected=True,
            )
        )
        assert verdict is not None
        assert verdict.reason == StopReason.GOAL_DRIFT.value


# ---------------------------------------------------------------------------
# AgentLoop 集成测试
# ---------------------------------------------------------------------------


class TestStepGuardInAgentLoop:
    def test_guard_initialized_in_loop(self, temp_workspace):
        """AgentLoop 构造时自动创建 StepGuard。"""
        from agent_runtime.agent_loop import AgentLoop
        from agent_runtime.config import AgentConfig
        from agent_runtime.providers.clients import FakeModelClient
        from agent_runtime.runtime import Agent
        from agent_runtime.workspace import WorkspaceContext

        config = AgentConfig(provider="fake", max_steps=3)
        ws = WorkspaceContext.build(str(temp_workspace))
        agent = Agent(
            config=config,
            model_client=FakeModelClient(outputs=["<final>ok</final>"]),
            workspace=ws,
        )
        loop = AgentLoop(agent)
        assert loop._step_guard is not None
        assert loop._step_guard.stall_count == 0

    def test_stall_marks_todo_blocked(self, temp_workspace):
        """连续 N 步无进展 → todo blocked + 停滞提示（不终止循环）。"""
        from agent_runtime.agent_loop import AgentLoop
        from agent_runtime.config import AgentConfig
        from agent_runtime.providers.clients import FakeModelClient
        from agent_runtime.runtime import Agent
        from agent_runtime.workspace import WorkspaceContext

        outputs = [
            '<tool>{"name":"write_file","args":{"path":"a.txt","content":"x"}}</tool>',
            '<tool>{"name":"write_file","args":{"path":"b.txt","content":"y"}}</tool>',
            '<tool>{"name":"write_file","args":{"path":"c.txt","content":"z"}}</tool>',
            "<final>done</final>",
        ]
        config = AgentConfig(provider="fake", max_steps=10, approval="auto")
        ws = WorkspaceContext.build(str(temp_workspace))
        agent = Agent(
            config=config,
            model_client=FakeModelClient(outputs=outputs),
            workspace=ws,
        )
        agent.dry_run = True  # dry_run → write_file 不实际写文件，无 affected_paths
        loop = AgentLoop(agent)

        loop._step_guard = StepGuard(stall_threshold=3)
        loop._step_guard.reset(task_summary="fix 问题", suspect_files=set())

        answer = loop.run("fix 问题")
        # stall 不终止，replan 提示注入到 tool 结果中
        history = agent.session.get("history", [])
        tool_msgs = [h["content"] for h in history if h.get("role") == "tool"]
        assert any("停滞" in m for m in tool_msgs), f"tool msgs: {tool_msgs[:3]}"
