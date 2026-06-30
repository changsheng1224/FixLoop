"""进度回调 + 降级链单测。"""

import io

from agent_runtime.callbacks import CLIProgressCallback
from agent_runtime.config import AgentConfig
from agent_runtime.providers.clients import FakeModelClient
from agent_runtime.runtime import Agent
from agent_runtime.workspace import WorkspaceContext


class TestCLIProgressCallback:
    """CLIProgressCallback 测试。"""

    def test_output_format(self):
        buf = io.StringIO()
        cb = CLIProgressCallback(output=buf)
        cb.on_step_start(1, 6)
        cb.on_tool_executed("read_file", "1 | class AgentConfig(BaseModel):\n2 | ...")
        output = buf.getvalue()
        assert "read_file" in output
        assert "✅" in output

    def test_error_shows_cross(self):
        buf = io.StringIO()
        cb = CLIProgressCallback(output=buf)
        cb.on_tool_executed("run_shell", "Error: command not found")
        output = buf.getvalue()
        assert "❌" in output

    def test_final_answer(self):
        buf = io.StringIO()
        cb = CLIProgressCallback(output=buf)
        cb.on_final_answer("问题已修复")
        output = buf.getvalue()
        assert "done" in output


class TestCallbackInAgentLoop:
    """callback 集成到 AgentLoop 测试。"""

    def test_callback_receives_events(self, temp_workspace):
        config = AgentConfig(provider="fake", max_steps=3)
        ws = WorkspaceContext.build(str(temp_workspace))
        client = FakeModelClient([
            '<tool>{"name":"list_files","args":{"path":"."}}</tool>',
            "<final>done</final>",
        ])
        agent = Agent(config=config, model_client=client, workspace=ws)

        buf = io.StringIO()
        cb = CLIProgressCallback(output=buf)

        # Agent.ask() 需要被扩展支持 callback 参数
        # 这里直接调 AgentLoop
        from agent_runtime.agent_loop import AgentLoop
        loop = AgentLoop(agent)
        answer = loop.run("list files", callback=cb)
        assert "done" in answer
        output = buf.getvalue()
        assert "list_files" in output
