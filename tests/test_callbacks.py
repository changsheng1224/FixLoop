"""进度回调 + AgentCallback 生命周期钩子单测（V1.4-Bonus2）。"""

from __future__ import annotations

import io

from agent_runtime.agent_loop import AgentLoop
from agent_runtime.callbacks import AgentCallback, CLIProgressCallback
from agent_runtime.config import AgentConfig
from agent_runtime.providers.clients import FakeModelClient
from agent_runtime.runtime import Agent
from agent_runtime.workspace import WorkspaceContext


# ---------------------------------------------------------------------------
# AgentCallback 基类
# ---------------------------------------------------------------------------


class TestAgentCallbackBase:
    """AgentCallback 基类：默认 no-op。"""

    def test_all_methods_noop_by_default(self):
        """所有 8 个钩子方法默认不抛异常。"""
        cb = AgentCallback()
        cb.on_step_start(1, 10, path="xml")
        cb.on_pre_model(1, "prompt...", path="xml")
        cb.on_post_model(1, "raw...", 100, path="xml")
        cb.on_pre_tool(1, "read_file", {"path": "f.py"}, path="xml")
        cb.on_post_tool(1, "read_file", "result...", 50, path="xml")
        cb.on_tool_executed(1, "read_file", "result...", 50, "OK")
        cb.on_react_phase("reasoning", 1, 10, tool="")
        cb.on_final_answer("done")

    def test_subclass_overrides_subset(self):
        """子类可只覆盖部分钩子。"""

        class MyCallback(AgentCallback):
            def __init__(self):
                self.events: list[str] = []

            def on_step_start(self, step, max_steps, *, path=""):
                self.events.append(f"step:{step}")

            def on_pre_tool(self, step, tool_name, tool_args, *, path=""):
                self.events.append(f"pre:{tool_name}")

        cb = MyCallback()
        cb.on_step_start(1, 5, path="xml")
        cb.on_pre_model(1, "p", path="xml")  # no-op fallback
        cb.on_pre_tool(1, "grep", {"pattern": "x"}, path="xml")
        assert cb.events == ["step:1", "pre:grep"]

    def test_path_passthrough(self):
        """path 参数正确透传到回调。"""

        class PathTracker(AgentCallback):
            def __init__(self):
                self.paths: list[str] = []

            def on_pre_model(self, step, prompt_preview, *, path=""):
                self.paths.append(f"pre_model:{path}")

            def on_post_model(self, step, raw_preview, elapsed_ms, *, path=""):
                self.paths.append(f"post_model:{path}")

            def on_pre_tool(self, step, tool_name, tool_args, *, path=""):
                self.paths.append(f"pre_tool:{path}")

            def on_post_tool(self, step, tool_name, result_preview, elapsed_ms, *, path=""):
                self.paths.append(f"post_tool:{path}")

            def on_step_start(self, step, max_steps, *, path=""):
                self.paths.append(f"step:{path}")

        cb = PathTracker()
        cb.on_pre_model(1, "...", path="xml")
        cb.on_pre_model(1, "...", path="native")
        cb.on_pre_tool(1, "grep", {}, path="xml")
        cb.on_pre_tool(1, "grep", {}, path="native")
        assert "pre_model:xml" in cb.paths
        assert "pre_model:native" in cb.paths
        assert "pre_tool:xml" in cb.paths
        assert "pre_tool:native" in cb.paths


# ---------------------------------------------------------------------------
# CLIProgressCallback（向后兼容）
# ---------------------------------------------------------------------------


class TestCLIProgressCallback:
    """CLIProgressCallback：继承 AgentCallback，终端输出不变。"""

    def test_is_agent_callback_subclass(self):
        """CLIProgressCallback 应继承 AgentCallback。"""
        assert issubclass(CLIProgressCallback, AgentCallback)

    def test_output_format(self):
        buf = io.StringIO()
        cb = CLIProgressCallback(output=buf)
        cb.on_step_start(1, 6, path="xml")
        cb.on_tool_executed(1, "read_file", "1 | class AgentConfig(BaseModel):\n2 | ...", 100, "OK")
        output = buf.getvalue()
        assert "read_file" in output
        assert "OK" in output

    def test_error_shows_fail(self):
        buf = io.StringIO()
        cb = CLIProgressCallback(output=buf)
        cb.on_tool_executed(1, "run_shell", "Error: command not found", 200, "FAIL")
        output = buf.getvalue()
        assert "FAIL" in output

    def test_dry_run_shows_dry(self):
        buf = io.StringIO()
        cb = CLIProgressCallback(output=buf)
        cb.on_tool_executed(1, "write_file", "[DRY RUN] would write...", 50, "DRY")
        output = buf.getvalue()
        assert "DRY" in output

    def test_final_answer(self):
        buf = io.StringIO()
        cb = CLIProgressCallback(output=buf)
        cb.on_final_answer("问题已修复")
        output = buf.getvalue()
        assert "done" in output or "问题已修复" in output

    def test_react_phase(self):
        buf = io.StringIO()
        cb = CLIProgressCallback(output=buf)
        cb.on_react_phase("reasoning", 1, 6)
        output = buf.getvalue()
        assert "reasoning" in output or "[1/6]" in output

    def test_inherited_noop_methods_dont_crash(self):
        """继承自 AgentCallback 的未覆盖方法不抛异常。"""
        buf = io.StringIO()
        cb = CLIProgressCallback(output=buf)
        # 新钩子：默认 no-op
        cb.on_pre_model(1, "prompt...", path="xml")
        cb.on_post_model(1, "raw...", 100, path="xml")
        cb.on_pre_tool(1, "read_file", {"path": "f.py"}, path="xml")
        cb.on_post_tool(1, "read_file", "result...", 50, path="xml")
        # 不应有 stderr 输出
        assert buf.getvalue() == ""


# ---------------------------------------------------------------------------
# AgentLoop 集成：全部 8 个钩子
# ---------------------------------------------------------------------------


class TestCallbackInAgentLoop:
    """AgentLoop callback 集成测试。"""

    def test_callback_receives_events(self, temp_workspace):
        """XML 路径：回调收到工具执行事件。"""
        config = AgentConfig(provider="fake", max_steps=3)
        ws = WorkspaceContext.build(str(temp_workspace))
        client = FakeModelClient([
            '<tool>{"name":"list_files","args":{"path":"."}}</tool>',
            "<final>done</final>",
        ])
        agent = Agent(config=config, model_client=client, workspace=ws)
        loop = AgentLoop(agent)

        buf = io.StringIO()
        cb = CLIProgressCallback(output=buf)
        answer = loop.run("list files", callback=cb)
        assert "done" in answer
        output = buf.getvalue()
        assert "list_files" in output

    def test_all_eight_hooks_fire_in_xml_path(self, temp_workspace):
        """XML 路径：全部 8 个钩子被调用。"""

        class HookTracker(AgentCallback):
            def __init__(self):
                self.called: set[str] = set()

            def on_step_start(self, step, max_steps, *, path=""):
                self.called.add("on_step_start")
            def on_pre_model(self, step, prompt_preview, *, path=""):
                self.called.add("on_pre_model")
            def on_post_model(self, step, raw_preview, elapsed_ms, *, path=""):
                self.called.add("on_post_model")
            def on_pre_tool(self, step, tool_name, tool_args, *, path=""):
                self.called.add("on_pre_tool")
            def on_post_tool(self, step, tool_name, result_preview, elapsed_ms, *, path=""):
                self.called.add("on_post_tool")
            def on_tool_executed(self, step, name, result_preview, elapsed_ms, status):
                self.called.add("on_tool_executed")
            def on_react_phase(self, phase, step, max_steps, *, tool=""):
                self.called.add("on_react_phase")
            def on_final_answer(self, text):
                self.called.add("on_final_answer")

        config = AgentConfig(provider="fake", max_steps=3)
        ws = WorkspaceContext.build(str(temp_workspace))
        client = FakeModelClient([
            '<tool>{"name":"read_file","args":{"path":"test.py"}}</tool>',
            "<final>done</final>",
        ])
        agent = Agent(config=config, model_client=client, workspace=ws)
        loop = AgentLoop(agent)

        tracker = HookTracker()
        answer = loop.run("read test.py", callback=tracker)
        assert "done" in answer

        expected = {
            "on_step_start", "on_pre_model", "on_post_model",
            "on_pre_tool", "on_post_tool", "on_tool_executed",
            "on_react_phase", "on_final_answer",
        }
        missing = expected - tracker.called
        assert not missing, f"未触发的钩子: {missing}"

    def test_all_eight_hooks_fire_in_native_path(self, temp_workspace):
        """Native 路径：全部 8 个钩子被调用。"""

        class HookTracker(AgentCallback):
            def __init__(self):
                self.called: set[str] = set()

            def on_step_start(self, step, max_steps, *, path=""):
                self.called.add("on_step_start")
            def on_pre_model(self, step, prompt_preview, *, path=""):
                self.called.add("on_pre_model")
            def on_post_model(self, step, raw_preview, elapsed_ms, *, path=""):
                self.called.add("on_post_model")
            def on_pre_tool(self, step, tool_name, tool_args, *, path=""):
                self.called.add("on_pre_tool")
            def on_post_tool(self, step, tool_name, result_preview, elapsed_ms, *, path=""):
                self.called.add("on_post_tool")
            def on_tool_executed(self, step, name, result_preview, elapsed_ms, status):
                self.called.add("on_tool_executed")
            def on_react_phase(self, phase, step, max_steps, *, tool=""):
                self.called.add("on_react_phase")
            def on_final_answer(self, text):
                self.called.add("on_final_answer")

        from agent_runtime.providers.clients import FakeNativeToolClient

        config = AgentConfig(provider="fake", max_steps=3)
        ws = WorkspaceContext.build(str(temp_workspace))
        # 需要 tool → final 序列才能触发全部 8 个钩子
        client = FakeNativeToolClient(outputs=[
            '<tool>{"name":"list_files","args":{"path":"."}}</tool>',
            "<final>done</final>",
        ])
        agent = Agent(config=config, model_client=client, workspace=ws)
        loop = AgentLoop(agent)

        tracker = HookTracker()
        answer = loop.run("read test.py", callback=tracker)
        assert answer

        expected = {
            "on_step_start", "on_pre_model", "on_post_model",
            "on_pre_tool", "on_post_tool", "on_tool_executed",
            "on_react_phase", "on_final_answer",
        }
        missing = expected - tracker.called
        assert not missing, f"未触发的钩子: {missing}"


# ---------------------------------------------------------------------------
# _notify 统一入口
# ---------------------------------------------------------------------------


class TestNotifyHelper:
    """_notify() 统一入口行为。"""

    def test_notify_none_callback_silent(self):
        """callback=None 时不抛异常。"""
        from agent_runtime.agent_loop import AgentLoop
        from agent_runtime.task_state import TaskState

        # 空 Agent 仅用于测试 _notify
        loop = AgentLoop.__new__(AgentLoop)
        loop.max_steps = 5

        # 不应抛异常
        loop._notify("on_step_start", None, step=1, max_steps=5, path="xml")
        loop._notify("on_pre_tool", None, step=1, tool_name="grep", tool_args={}, path="xml")

    def test_notify_missing_method_silent(self):
        """回调未实现的方法静默跳过。"""
        from agent_runtime.agent_loop import AgentLoop

        loop = AgentLoop.__new__(AgentLoop)
        loop.max_steps = 5

        cb = AgentCallback()  # 所有方法 no-op
        # 不应抛异常
        loop._notify("on_pre_model", cb, step=1, prompt_preview="x", path="xml")


# ---------------------------------------------------------------------------
# 向后兼容：ProgressCallback 别名
# ---------------------------------------------------------------------------


class TestProgressCallbackAlias:
    """ProgressCallback = AgentCallback 向后兼容。"""

    def test_alias_is_agent_callback(self):
        from agent_runtime.callbacks import ProgressCallback

        assert ProgressCallback is AgentCallback

    def test_cli_callback_is_progress_callback(self):
        from agent_runtime.callbacks import ProgressCallback

        assert issubclass(CLIProgressCallback, ProgressCallback)
