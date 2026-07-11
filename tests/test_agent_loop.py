"""AgentLoop + Agent.ask() 单测：控制循环的完整验证。

使用 FakeModelClient 预设输出序列，不调真实 API。
"""

import pytest

from agent_runtime.config import AgentConfig
from agent_runtime.providers.clients import FakeModelClient, FakeNativeToolClient
from agent_runtime.runtime import Agent


@pytest.fixture
def config():
    return AgentConfig(provider="fake", max_steps=3, max_new_tokens=256)


def _make_agent(outputs: list[str], config, workspace):
    """快速创建一个使用 FakeClient 的 Agent。"""
    client = FakeModelClient(outputs)
    return Agent(config=config, model_client=client, workspace=workspace)


class TestAgentAsk:
    """Agent.ask() 集成测试。"""

    def test_single_tool_then_final(self, config, workspace):
        """Agent 调一次 tool 后返回 final。"""
        agent = _make_agent(
            [
                '<tool>{"name":"list_files","args":{"path":"."}}</tool>',
                "<final>找到 2 个文件</final>",
            ],
            config,
            workspace,
        )
        answer = agent.ask("列出文件")
        assert "找到" in answer
        assert agent.tool_context is not None

    def test_multi_tool_chain(self, config, workspace):
        """Agent 连续调用 3 次 tool 后返回 final。"""
        outputs = [
            '<tool>{"name":"list_files","args":{"path":"."}}</tool>',
            '<tool>{"name":"read_file","args":{"path":"README.md"}}</tool>',
            '<tool>{"name":"search","args":{"pattern":"test","path":"."}}</tool>',
            "<final>搜索完成，共找到 5 处匹配</final>",
        ]
        agent = _make_agent(outputs, config, workspace)
        answer = agent.ask("全面分析")
        assert "搜索" in answer

    def test_xml_loop_passes_prompt_cache_key_each_step(self, config, workspace):
        """XML 路径每 step 向 ModelClient 传递 prompt_cache_key。"""
        outputs = [
            '<tool>{"name":"list_files","args":{"path":"."}}</tool>',
            "<final>done</final>",
        ]
        client = FakeModelClient(outputs)
        agent = Agent(config=config, model_client=client, workspace=workspace)
        agent.ask("scan files")
        assert len(client.cache_keys) == 2
        assert client.cache_keys[0]
        assert client.cache_keys[0] == client.cache_keys[1]

    def test_xml_loop_context_prefix_aligned_in_trace(self, config, workspace, temp_workspace):
        """多 step build 时 trace context_built 报告 prefix_aligned。"""
        import json

        from agent_runtime.run_store import RunStore

        outputs = [
            '<tool>{"name":"list_files","args":{"path":"."}}</tool>',
            "<final>done</final>",
        ]
        agent = _make_agent(outputs, config, workspace)
        agent.cwd = str(temp_workspace)
        agent.ask("scan")

        run_dirs = list(RunStore(str(temp_workspace)).runs_dir.iterdir())
        trace_path = run_dirs[0] / "trace.jsonl"
        built = [
            json.loads(line)
            for line in trace_path.read_text(encoding="utf-8").strip().splitlines()
            if json.loads(line).get("event") == "context_built"
        ]
        assert len(built) >= 2
        second = built[1].get("payload", {})
        assert second.get("prefix_aligned") is True
        assert second.get("projection_step") == 2

    def test_final_only_no_tools(self, config, workspace):
        """Agent 直接返回 final，不调任何工具。"""
        agent = _make_agent(
            ["<final>你好！我来帮你分析代码。</final>"],
            config,
            workspace,
        )
        answer = agent.ask("你好")
        assert "你好" in answer


class TestAgentLoopStopConditions:
    """AgentLoop 停机条件测试。"""

    def test_stops_at_max_steps(self, config, workspace):
        """达到 max_steps 后强制停机。"""
        # 一直返回 tool 调用，永不返回 final
        infinite_tools = [
            '<tool>{"name":"list_files","args":{"path":"."}}</tool>',
            '<tool>{"name":"list_files","args":{"path":"."}}</tool>',
            '<tool>{"name":"list_files","args":{"path":"."}}</tool>',
            '<tool>{"name":"list_files","args":{"path":"."}}</tool>',
            '<tool>{"name":"list_files","args":{"path":"."}}</tool>',
        ]
        agent = _make_agent(infinite_tools, config, workspace)
        answer = agent.ask("一直循环")
        # FakeClient 耗尽会抛 RuntimeError，但如果 max_steps 先到则正常停机
        # max_steps=3，3 次 tool 后应停机
        assert "步数限制" in answer or "maximum tool steps" in answer.lower()

    def test_retry_on_bad_format(self, config, workspace):
        """格式错误 → retry → 最后返回 final。"""
        outputs = [
            "garbage output without proper format",
            '<tool>{"name":"list_files","args":{"path":"."}}</tool>',
            "<final>重试后成功了</final>",
        ]
        agent = _make_agent(outputs, config, workspace)
        answer = agent.ask("测试重试")
        assert "成功" in answer
        # 应该有 retry 记录
        history_roles = [h["role"] for h in agent.session["history"]]
        assert "system" in history_roles  # retry 通知以 system 角色记录
        system_msgs = [
            h["content"] for h in agent.session["history"] if h["role"] == "system"
        ]
        assert any("解析失败" in m for m in system_msgs)

    def test_parse_retry_emits_trace(self, config, workspace, temp_workspace):
        """解析失败 recovery 写入 trace parse_retry 事件。"""
        import json

        from agent_runtime.run_store import RunStore

        outputs = [
            '<tool>{"name":"list_files","args":{"path": ".</tool>',
            '<tool>{"name":"list_files","args":{"path":"."}}</tool>',
            "<final>caret 重试成功</final>",
        ]
        agent = _make_agent(outputs, config, workspace)
        agent.cwd = str(temp_workspace)
        answer = agent.ask("测试 caret trace")
        assert "成功" in answer

        run_dirs = list(RunStore(str(temp_workspace)).runs_dir.iterdir())
        trace_path = run_dirs[0] / "trace.jsonl"
        events = [
            json.loads(line)
            for line in trace_path.read_text(encoding="utf-8").strip().splitlines()
        ]
        parse_retries = [e for e in events if e.get("event") == "parse_retry"]
        assert len(parse_retries) == 1
        payload = parse_retries[0]["payload"]
        assert payload["kind"] == "json_in_tool"
        assert payload["attempt"] == 1
        system_msgs = [
            h["content"] for h in agent.session["history"] if h["role"] == "system"
        ]
        assert any("^" in m for m in system_msgs)

    def test_circuit_breaker_events_in_trace(self, config, workspace, temp_workspace):
        """跨多次 ask 累积失败与半开恢复写入 circuit_* trace 事件。"""
        import json
        import time

        from agent_runtime.providers.circuit_breaker import CircuitBreaker
        from agent_runtime.run_store import RunStore

        class FailingClient(FakeModelClient):
            def complete(self, prompt: str, max_new_tokens: int = 512, prompt_cache_key: str = ""):
                raise RuntimeError("simulated API 500")

        agent = Agent(
            config=AgentConfig(provider="fake", max_steps=3, max_new_tokens=256),
            model_client=FailingClient([]),
            workspace=workspace,
        )
        agent.cwd = str(temp_workspace)
        agent.circuit_breaker = CircuitBreaker(
            failure_threshold=2,
            recovery_timeout=0.05,
            half_open_success_threshold=1,
        )
        store = RunStore(str(temp_workspace))

        for _ in range(2):
            with pytest.raises(RuntimeError):
                agent.ask("trigger breaker")

        run_dirs = sorted(store.runs_dir.iterdir(), key=lambda p: p.stat().st_mtime)
        events = [
            json.loads(line)
            for line in (run_dirs[-1] / "trace.jsonl").read_text(encoding="utf-8").strip().splitlines()
        ]
        opened = [e for e in events if e.get("event") == "circuit_opened"]
        assert len(opened) == 1
        assert opened[0]["payload"]["reason"] == "consecutive_failures"

        time.sleep(0.1)
        agent.model_client = FakeModelClient(["<final>recovered</final>"])
        agent.ask("recover")

        run_dirs = sorted(store.runs_dir.iterdir(), key=lambda p: p.stat().st_mtime)
        events = [
            json.loads(line)
            for line in (run_dirs[-1] / "trace.jsonl").read_text(encoding="utf-8").strip().splitlines()
        ]
        event_names = [e.get("event") for e in events]
        assert "half_open_probe" in event_names
        assert "circuit_closed" in event_names

    def test_invalid_tool_payload_emits_parse_retry(self, config, workspace, temp_workspace):
        """tool payload 缺 name → parse_recovery + parse_retry trace。"""
        import json

        from agent_runtime.run_store import RunStore

        outputs = [
            '<tool>{"args":{"path":"."}}</tool>',
            '<tool>{"name":"list_files","args":{"path":"."}}</tool>',
            "<final>修复成功</final>",
        ]
        agent = _make_agent(outputs, config, workspace)
        agent.cwd = str(temp_workspace)
        answer = agent.ask("测试 invalid tool")
        assert "成功" in answer

        run_dirs = list(RunStore(str(temp_workspace)).runs_dir.iterdir())
        events = [
            json.loads(line)
            for line in (run_dirs[0] / "trace.jsonl").read_text(encoding="utf-8").strip().splitlines()
        ]
        retries = [e for e in events if e.get("event") == "parse_retry"]
        assert len(retries) == 1
        assert retries[0]["payload"]["kind"] == "invalid_tool_payload"

    def test_empty_model_response(self, config, workspace):
        """空响应被视为格式错误 → retry。"""
        outputs = [
            "",
            "<final>第二次成功了</final>",
        ]
        agent = _make_agent(outputs, config, workspace)
        answer = agent.ask("测试空响应")
        assert "成功" in answer


class TestAgentSession:
    """Agent 会话状态测试。"""

    def test_history_accumulates(self, config, workspace):
        """多轮 ask 后 history 累积。"""
        agent = _make_agent(
            [
                "<final>第一轮</final>",
                "<final>第二轮</final>",
            ],
            config,
            workspace,
        )
        agent.ask("问1")
        agent.ask("问2")
        # 应该有 2 轮交互的历史
        history = agent.session["history"]
        user_msgs = [h for h in history if h["role"] == "user"]
        assert len(user_msgs) == 2

    def test_unknown_tool_handled(self, config, workspace):
        """调用未注册工具 → 返回 Error 信息 → 继续循环。"""
        outputs = [
            '<tool>{"name":"non_existent_tool","args":{}}</tool>',
            "<final>工具不存在，但已优雅处理</final>",
        ]
        agent = _make_agent(outputs, config, workspace)
        answer = agent.ask("调未知工具")
        assert "处理" in answer


class TestCompleteOnce:
    def test_uses_system_prompt_without_agent_loop(self, config, workspace):
        client = FakeModelClient(["<final>[]</final>"])
        agent = Agent(
            config=config,
            model_client=client,
            workspace=workspace,
            system_prompt="You are patcher",
        )
        result = agent.complete_once("fix the bug")
        assert result == "<final>[]</final>"
        assert len(client.prompts) == 1
        assert "You are patcher" in client.prompts[0]
        assert "fix the bug" in client.prompts[0]

    def test_system_prompt_override(self, config, workspace):
        client = FakeModelClient(["ok"])
        agent = Agent(
            config=config,
            model_client=client,
            workspace=workspace,
            system_prompt="base system",
        )
        agent.complete_once("user task", system_prompt="type_error variant")
        assert client.prompts[0].startswith("type_error variant\n\nuser task")


class TestNativeToolsTokenUsage:
    def test_chat_with_tools_returns_call_usage(self, config, workspace):
        client = FakeNativeToolClient(
            [
                '<tool>{"name":"list_files","args":{"path":"."}}</tool>',
                "<final>done</final>",
            ]
        )
        agent = _make_agent([], config, workspace)
        agent.model_client = client

        def executor(name, args):
            return "ok"

        answer, usage = client.chat_with_tools(
            system_prompt="sys",
            user_message="go",
            tools=[{"name": "list_files", "description": "", "input_schema": {"type": "object"}}],
            executor=executor,
        )
        assert answer == "done"
        assert usage["calls"] == 2
        assert usage["total_tokens"] > 0 if "total_tokens" in usage else (
            usage["input_tokens"] + usage["output_tokens"] > 0
        )

    def test_shared_run_agent_report_includes_api_tokens(self, config, workspace, temp_workspace):
        import json

        from agent_runtime.run_store import RunStore

        client = FakeNativeToolClient(["<final>ok</final>"])
        agent = Agent(
            config=config,
            model_client=client,
            workspace=workspace,
            cwd=str(temp_workspace),
            agent_name="localizer",
        )
        agent.shared_run_id = "repair-test-token"
        agent.ask("locate bug")

        report_path = (
            RunStore(str(temp_workspace)).runs_dir
            / "repair-test-token"
            / "agent_report.localizer.json"
        )
        data = json.loads(report_path.read_text(encoding="utf-8"))
        assert data["total_tokens"] > 0
        assert data["api_calls"] >= 1
        assert data["cache_read_tokens"] == 0
        assert data["cache_creation_tokens"] == 0
        assert data["cache_hit_rate"] == 0.0


class TestTtftObservability:
    def test_native_tools_report_includes_ttft(self, config, workspace, temp_workspace):
        import json

        from agent_runtime.run_store import RunStore

        client = FakeNativeToolClient(["<final>ok</final>"])
        agent = Agent(
            config=config,
            model_client=client,
            workspace=workspace,
            cwd=str(temp_workspace),
            agent_name="localizer",
        )
        agent.shared_run_id = "repair-ttft-test"
        agent.ask("locate bug")

        report_path = (
            RunStore(str(temp_workspace)).runs_dir
            / "repair-ttft-test"
            / "agent_report.localizer.json"
        )
        data = json.loads(report_path.read_text(encoding="utf-8"))
        assert data["ttft_ms_p50"] == 0
        assert data["ttft_ms_last"] == 0
        assert len(data["ttft_ms_by_call"]) == 1
        assert data["ttft_ms_by_call"][0]["total_ms"] == 0

    def test_trace_includes_model_first_token(self, config, workspace, temp_workspace):
        import json

        from agent_runtime.run_store import RunStore

        client = FakeNativeToolClient(["<final>ok</final>"])
        agent = Agent(
            config=config,
            model_client=client,
            workspace=workspace,
            cwd=str(temp_workspace),
            agent_name="localizer",
        )
        agent.shared_run_id = "repair-ttft-trace"
        agent.ask("go")

        trace_path = (
            RunStore(str(temp_workspace)).runs_dir / "repair-ttft-trace" / "trace.jsonl"
        )
        events = [
            json.loads(line)["event"]
            for line in trace_path.read_text(encoding="utf-8").strip().splitlines()
        ]
        assert "model_request_start" in events
        assert "model_first_token" in events
        assert "model_complete" in events

    def test_text_parsing_report_includes_ttft(self, config, workspace, temp_workspace):
        import json

        from agent_runtime.run_store import RunStore

        agent = _make_agent(["<final>done</final>"], config, workspace)
        agent.cwd = str(temp_workspace)
        agent.ask("hello")

        run_dirs = list(RunStore(str(temp_workspace)).runs_dir.iterdir())
        report_path = run_dirs[0] / "report.json"
        data = json.loads(report_path.read_text(encoding="utf-8"))
        assert "ttft_ms_p50" in data
        assert data["ttft_ms_by_call"][0]["ttft_ms"] == 0

    def test_report_includes_context_summary(self, config, workspace, temp_workspace):
        import json

        from agent_runtime.run_store import RunStore

        agent = _make_agent(["<final>done</final>"], config, workspace)
        agent.cwd = str(temp_workspace)
        agent.ask("find the bug")

        run_dirs = list(RunStore(str(temp_workspace)).runs_dir.iterdir())
        report_path = run_dirs[0] / "report.json"
        data = json.loads(report_path.read_text(encoding="utf-8"))

        cs = data.get("context_summary")
        assert cs is not None, "report.json missing context_summary"
        assert cs["build_count"] >= 1
        assert isinstance(cs["sections"], dict)
        assert "cache_hit_rate" in cs
        assert 0.0 <= cs["cache_hit_rate"] <= 1.0

        rs = data.get("retry_summary")
        assert rs is not None, "report.json missing retry_summary"
        assert "parse_retries" in rs
        assert "model_attempts" in rs
        assert "tool_steps" in rs
        assert rs["model_attempts"] >= 1
        assert rs["tool_steps"] >= 0
