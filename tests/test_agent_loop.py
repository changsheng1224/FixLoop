"""AgentLoop + Agent.ask() 单测：控制循环的完整验证。

使用 FakeModelClient 预设输出序列，不调真实 API。
"""

import tempfile

import pytest

from agent_runtime.config import AgentConfig
from agent_runtime.providers.clients import FakeModelClient, FakeNativeToolClient
from agent_runtime.runtime import Agent
from agent_runtime.workspace import WorkspaceContext


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
        assert second.get("prefix_aligned") in (True, False)  # state 段可能变化
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
        assert any("④" in m for m in system_msgs)  # 四段式 prompt

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

        qu = data.get("quota_usage")
        assert qu is not None, "report.json missing quota_usage"
        assert qu["total"]["used"] >= 0
        assert qu["total"]["limit"] == 50


# ---------------------------------------------------------------------------
# final_answer schema 校验（V1.4-Bonus2c）
# ---------------------------------------------------------------------------


class TestFinalAnswerValidation:
    """final_answer JSON schema 校验 + 重试。"""

    def test_valid_json_passes(self, config, workspace):
        """合法 JSON final answer 直接通过。"""
        config.json_mode = True
        agent = _make_agent(
            ['<final>{"file_path":"app.py","line":42}</final>'],
            config, workspace,
        )
        answer = agent.ask("find bug")
        assert "file_path" in answer
        assert "app.py" in answer

    def test_invalid_json_retries(self, config, workspace):
        """非法 JSON → recovery prompt → 模型重试。"""
        config.json_mode = True
        agent = _make_agent(
            [
                "<final>not json</final>",           # ← 第 1 次失败
                '<final>{"file_path":"app.py"}</final>',  # ← 重试成功
            ],
            config, workspace,
        )
        answer = agent.ask("find bug")
        # 最终应接受第二次合法输出
        assert "file_path" in answer

    def test_invalid_json_exhausted_retries(self, config, workspace):
        """重试耗尽后接受原样（不无限循环）。"""
        config.json_mode = True
        agent = _make_agent(
            [
                "<final>bad1</final>",
                "<final>bad2</final>",
                "<final>bad3</final>",  # 第 3 次 — 重试耗尽
            ],
            config, workspace,
        )
        answer = agent.ask("find bug")
        # 耗尽后接受最后一次输出
        assert "bad3" in answer

    def test_schema_missing_fields_retries(self, config, workspace):
        """缺少必填字段 → recovery prompt 含字段名。"""
        config.json_mode = True
        config.final_schema = {"file_path": "str", "line": "int"}
        agent = _make_agent(
            [
                '<final>{"file_path":"app.py"}</final>',  # ← 缺 line
                '<final>{"file_path":"app.py","line":42}</final>',  # ← 补齐后通过
            ],
            config, workspace,
        )
        answer = agent.ask("find bug")
        assert "line" in answer

    def test_schema_wrong_type_retries(self, config, workspace):
        """字段类型错误 → recovery prompt 含类型信息。"""
        config.json_mode = True
        config.final_schema = {"file_path": "str", "line": "int"}
        agent = _make_agent(
            [
                '<final>{"file_path":"app.py","line":"not_a_number"}</final>',
                '<final>{"file_path":"app.py","line":42}</final>',
            ],
            config, workspace,
        )
        answer = agent.ask("find bug")
        assert '"line":42' in answer or '"line": 42' in answer

    def test_no_schema_passes_any_json(self, config, workspace):
        """无 final_schema 时仅校验 JSON 语法。"""
        config.json_mode = True
        # 无 schema → 任意合法 JSON 都通过
        agent = _make_agent(
            ['<final>{"any":"thing","foo":123}</final>'],
            config, workspace,
        )
        answer = agent.ask("do something")
        assert "any" in answer

    def test_json_retry_emits_trace(self, config, workspace):
        """JSON 重试发出 trace 事件。"""
        from agent_runtime.agent_loop import AgentLoop

        config.json_mode = True
        client = FakeModelClient([
            "<final>bad</final>",
            '<final>{"ok":true}</final>',
        ])
        agent = Agent(config=config, model_client=client, workspace=workspace)
        loop = AgentLoop(agent)

        events = []
        loop._emit = lambda name, data=None: events.append((name, data))
        answer = loop.run("test")

        assert "ok" in answer
        json_retries = [e for e in events if e[0] == "json_retry"]
        assert len(json_retries) == 1
        assert json_retries[0][1]["attempt"] == 1

    def test_non_json_mode_skips_validation(self, config, workspace):
        """非 json_mode 时跳过校验，即使配置了 schema。"""
        config.json_mode = False
        config.final_schema = {"file_path": "str"}
        agent = _make_agent(
            ["<final>this is plain text, not json</final>"],
            config, workspace,
        )
        answer = agent.ask("explain")
        # 纯文本答案直接通过
        assert "plain text" in answer

    def test_json_array_final_passes(self, config, workspace):
        """JSON 数组（如 SuspectList）也通过语法校验。"""
        config.json_mode = True
        agent = _make_agent(
            ['<final>[{"file":"a.py","line":1}]</final>'],
            config, workspace,
        )
        answer = agent.ask("find bugs")
        assert "a.py" in answer

    def test_final_answer_failure_returns_to_acting(self, config, workspace):
        """畸形 final → ParseRetry → 再次 model 调用而非结束（V1.5-Bonus1i）。"""
        from agent_runtime.agent_loop import AgentLoop

        config.json_mode = True
        agent = _make_agent(
            [
                "<final>bad json</final>",          # 失败 → retry
                '<final>{"ok":true}</final>',       # 成功
            ],
            config, workspace,
        )
        loop = AgentLoop(agent)
        events = []

        def capture(name, data=None):
            events.append(name)

        loop._emit = capture
        answer = loop.run("test")
        assert "ok" in answer
        # 应有 json_retry 事件，且不应直接结束
        assert "json_retry" in events


# ---------------------------------------------------------------------------
# CoT 提取（V1.4-Bonus2d）
# ---------------------------------------------------------------------------


class TestCoTStripping:
    """_strip_cot 思考内容剥离。"""

    def test_strips_think_tags(self):
        """移除 <think>...</think> 标签块。"""
        from agent_runtime.agent_loop import AgentLoop

        raw = "<think>Let me analyze first...</think>\n<final>done</final>"
        cleaned = AgentLoop._strip_cot(raw)
        assert "<think>" not in cleaned
        assert "analyze" not in cleaned
        assert "<final>done</final>" in cleaned

    def test_strips_text_before_first_tag(self):
        """移除第一个结构化标签前的自然语言前缀。"""
        from agent_runtime.agent_loop import AgentLoop

        raw = "I need to read the file first.\n\n<tool>{\"name\":\"read_file\",\"args\":{\"path\":\"app.py\"}}</tool>"
        cleaned = AgentLoop._strip_cot(raw)
        assert "I need to read" not in cleaned
        assert "<tool>" in cleaned

    def test_preserves_plain_final_when_no_tags(self):
        """纯文本 final answer（无标签）：保留原样。"""
        from agent_runtime.agent_loop import AgentLoop

        raw = "The bug is in app.py line 42."
        cleaned = AgentLoop._strip_cot(raw)
        assert cleaned == raw

    def test_strips_think_and_prefix_together(self):
        """同时移除 <think> 和前缀文本。"""
        from agent_runtime.agent_loop import AgentLoop

        raw = (
            "<think>reasoning step 1</think>\n"
            "Now I'll search for the error...\n"
            "<tool>{\"name\":\"search\",\"args\":{\"pattern\":\"error\"}}</tool>"
        )
        cleaned = AgentLoop._strip_cot(raw)
        assert "reasoning" not in cleaned
        assert "Now I'll search" not in cleaned
        assert "<tool>" in cleaned

    def test_empty_after_strip_returns_original(self):
        """清洗后为空时回退到原始文本。"""
        from agent_runtime.agent_loop import AgentLoop

        raw = "<think>only thinking, no action</think>"
        cleaned = AgentLoop._strip_cot(raw)
        # 清洗后只剩空白 → 返回原始文本
        assert "only thinking" in cleaned

    def test_multiline_think_tag(self):
        """多行 <think> 块正确剥离。"""
        from agent_runtime.agent_loop import AgentLoop

        raw = "<think>\nline 1\nline 2\nline 3\n</think>\n<final>{\"ok\":true}</final>"
        cleaned = AgentLoop._strip_cot(raw)
        assert "line 1" not in cleaned
        assert "line 2" not in cleaned
        assert "ok" in cleaned

    def test_stripping_in_agent_ask(self, config, workspace):
        """Agent.ask() 端到端：CoT 被剥离，不进 history。"""
        agent = _make_agent(
            [
                '<tool>{"name":"read_file","args":{"path":"app.py"}}</tool>',
                "Now I see the bug. Let me fix it.\n<final>fixed</final>",
            ],
            config, workspace,
        )
        answer = agent.ask("find and fix bug")
        assert "fixed" in answer
        # history 中不应包含 CoT 前缀
        history = agent.session.get("history", [])
        for h in history:
            content = str(h.get("content", ""))
            if h.get("role") == "assistant":
                # 不应包含思考前缀
                assert "Now I see the bug" not in content

    def test_final_only_without_prefix_unchanged(self, config, workspace):
        """无 CoT 的 final answer 完全不变。"""
        agent = _make_agent(
            ["<final>fixed</final>"],
            config, workspace,
        )
        answer = agent.ask("fix bug")
        assert answer == "fixed"


# ---------------------------------------------------------------------------
# 死循环检测（V1.5-Bonus1）
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Plan/TodoList 强化（V1.5-Bonus1b）
# ---------------------------------------------------------------------------


class TestPlanPhase:
    def test_plan_phase_emits_events(self, config, workspace):
        """普通 ask 产生 plan_phase + plan_created 事件。"""
        from agent_runtime.agent_loop import AgentLoop

        agent = _make_agent(["<final>done</final>"], config, workspace)
        loop = AgentLoop(agent)
        events = []

        def capture(name, data=None):
            events.append(name)

        loop._emit = capture
        answer = loop.run("fix the bug in app.py")
        assert "done" in answer
        assert "plan_phase" in events
        assert "plan_created" in events

    def test_skip_plan_no_llm_call(self, config, workspace):
        """skip_plan=True 时不生成 plan，无 plan_created。"""
        from agent_runtime.agent_loop import AgentLoop

        agent = _make_agent(["<final>done</final>"], config, workspace)
        loop = AgentLoop(agent)
        events = []

        def capture(name, data=None):
            events.append(name)

        loop._emit = capture
        answer = loop.run("fix the bug", skip_plan=True)
        assert "done" in answer
        assert "plan_phase" in events  # 应有 skipped 事件
        assert "plan_created" not in events  # 无实际 plan

    def test_skip_plan_emits_skipped_trace(self, config, workspace):
        """skip_plan 时 plan_phase 携带 source=skipped。"""
        from agent_runtime.agent_loop import AgentLoop

        agent = _make_agent(["<final>done</final>"], config, workspace)
        loop = AgentLoop(agent)
        events = []

        def capture(name, data=None):
            events.append((name, data))

        loop._emit = capture
        loop.run("fix bug", skip_plan=True)
        plan_events = [e for e in events if e[0] == "plan_phase"]
        assert len(plan_events) == 1
        assert plan_events[0][1]["source"] == "skipped"

    def test_agent_ask_passes_skip_plan(self, config, workspace):
        """Agent.ask(skip_plan=True) 传递到 loop.run()。"""
        agent = _make_agent(["<final>done</final>"], config, workspace)
        # 通过 Agent.ask 接口
        answer = agent.ask("fix bug", skip_plan=True)
        assert "done" in answer


# ---------------------------------------------------------------------------
# 空模型响应 → 重试（V1.5-Bonus1c）
# ---------------------------------------------------------------------------


class TestEmptyModelResponse:
    def test_empty_then_success(self, config, workspace):
        """一次空响应后重试成功。"""
        outputs = [
            "",  # 空响应
            "<final>fixed</final>",
        ]
        agent = _make_agent(outputs, config, workspace)
        answer = agent.ask("fix bug")
        assert "fixed" in answer

    def test_consecutive_empty_stops_with_api_error(self, config, workspace):
        """连续空响应 → api_error。"""
        from agent_runtime.agent_loop import AgentLoop

        outputs = ["", "", "", "<final>never</final>"]
        agent = _make_agent(outputs, config, workspace)
        loop = AgentLoop(agent)
        answer = loop.run("fix bug")
        assert "API" in answer or "api_error" in loop.stop_reason or "空" in answer


class TestLoopDetection:
    def test_loop_detected_stops_with_circuit_breaker(self, config, workspace):
        """连续 3 次相同 read_file → circuit_breaker stop。"""
        from agent_runtime.agent_loop import AgentLoop

        config.loop_detect_threshold = 3
        agent = _make_agent(
            [
                '<tool>{"name":"read_file","args":{"path":"app.py"}}</tool>',
                '<tool>{"name":"read_file","args":{"path":"app.py"}}</tool>',
                '<tool>{"name":"read_file","args":{"path":"app.py"}}</tool>',
                "<final>done</final>",
            ],
            config, workspace,
        )
        loop = AgentLoop(agent)
        answer = loop.run("read app.py three times")
        assert "死循环" in answer or "circuit" in loop.stop_reason

    def test_different_args_no_loop_detection(self, config, workspace):
        """不同 path → 不触发死循环。"""
        from agent_runtime.agent_loop import AgentLoop

        config.loop_detect_threshold = 3
        agent = _make_agent(
            [
                '<tool>{"name":"read_file","args":{"path":"a.py"}}</tool>',
                '<tool>{"name":"read_file","args":{"path":"b.py"}}</tool>',
                '<tool>{"name":"read_file","args":{"path":"c.py"}}</tool>',
                "<final>done</final>",
            ],
            config, workspace,
        )
        loop = AgentLoop(agent)
        answer = loop.run("read files")
        assert "done" in answer


# ---------------------------------------------------------------------------
# 流式模型 cancel（V1.5-Bonus1e）
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Native context_built 对齐（V1.5-Bonus1h）
# ---------------------------------------------------------------------------


class TestNativeContextBuilt:
    def test_native_context_built_event_exists(self, config, workspace):
        """Native 路径 emit context_built 事件且含 sections 键。"""
        from agent_runtime.agent_loop import AgentLoop

        agent = _make_agent(
            ["<final>done</final>"],
            config, workspace,
        )
        # 使用 NativeFakeClient 走 Native 路径
        from agent_runtime.providers.clients import FakeNativeToolClient

        agent2 = Agent(
            config=config,
            model_client=FakeNativeToolClient(outputs=["<final>done</final>"]),
            workspace=workspace,
            cwd=str(workspace.repo_root),
        )
        loop = AgentLoop(agent2)
        events = []

        def capture(name, data=None):
            events.append((name, data))

        loop._emit = capture
        answer = loop.run("test native context built")
        assert "done" in answer

        ctx_events = [e for e in events if e[0] == "context_built"]
        assert len(ctx_events) >= 1, f"expected context_built event, got events: {[e[0] for e in events]}"
        payload = ctx_events[0][1]
        assert "total_tokens" in payload or "context_sections" in payload

    def test_native_long_history_triggers_compression(self, config, workspace):
        """Native 路径：长 history 触发压缩管线 metadata / compression_triggered。"""
        from agent_runtime.agent_loop import AgentLoop
        from agent_runtime.providers.clients import FakeNativeToolClient

        # 极小 budget → 压缩阈值降低，少量 history 即可触发
        config.prompt_budget = 2000
        # Native path 每轮调一次 complete()，多给几个输出
        agent = Agent(
            config=config,
            model_client=FakeNativeToolClient(
                outputs=["<final>done</final>"] * 5
            ),
            workspace=workspace,
            cwd=str(workspace.repo_root),
        )
        # 注入足够长的 history
        for i in range(30):
            agent.record({"role": "user", "content": f"question {i} " + "detail " * 20})
            agent.record({"role": "tool", "content": f"result {i}: " + "x" * 200})

        loop = AgentLoop(agent)
        events = []

        def capture(name, data=None):
            events.append((name, data))

        loop._emit = capture
        answer = loop.run("test native compression")
        assert "done" in answer

        # 验证压缩事件被 emit
        compression_events = [e for e in events if e[0] == "compression_triggered"]
        assert len(compression_events) >= 1, (
            f"expected compression_triggered events, got events: {[e[0] for e in events]}"
        )

    def test_native_and_xml_share_compression_pipeline(self, config, workspace):
        """Native 与 XML 路径共用同一压缩管线入口 run_compression_pipeline。"""
        from agent_runtime.agent_loop import AgentLoop
        from agent_runtime.providers.clients import FakeNativeToolClient

        cfg = AgentConfig(provider="fake", max_steps=3, max_new_tokens=256, prompt_budget=2000)

        # === Native 路径 ===
        agent_native = Agent(
            config=cfg,
            model_client=FakeNativeToolClient(outputs=["<final>native done</final>"] * 5),
            workspace=workspace,
            cwd=str(workspace.repo_root),
        )
        for i in range(30):
            agent_native.record({"role": "user", "content": f"q {i} " + "pad " * 15})
            agent_native.record({"role": "tool", "content": f"r {i}: " + "y" * 200})

        loop_native = AgentLoop(agent_native)
        native_events = []
        loop_native._emit = lambda n, d=None: native_events.append((n, d))
        loop_native.run("native compression test")

        # === XML 路径 ===
        agent_xml = _make_agent(
            ["<final>xml done</final>"] * 3,
            cfg, workspace,
        )
        for i in range(30):
            agent_xml.record({"role": "user", "content": f"q {i} " + "pad " * 15})
            agent_xml.record({"role": "tool", "content": f"r {i}: " + "y" * 200})

        loop_xml = AgentLoop(agent_xml)
        xml_events = []
        loop_xml._emit = lambda n, d=None: xml_events.append((n, d))
        loop_xml.run("xml compression test")

        # 两条路径都应触发 compression_triggered
        native_comp = [e for e in native_events if e[0] == "compression_triggered"]
        xml_comp = [e for e in xml_events if e[0] == "compression_triggered"]
        assert len(native_comp) >= 1, "Native path should emit compression_triggered"
        assert len(xml_comp) >= 1, "XML path should emit compression_triggered"


class TestStreamingCancel:
    def test_cancel_during_stream_stops_early(self):
        """流式输出中途 cancel → CancelledError → user_cancel stop。"""
        from agent_runtime.agent_loop import AgentLoop
        from agent_runtime.cancellation import CancellationToken

        class MockStreamClient:
            """流式 mock：模拟 chunk 输出并在中途被 cancel。"""
            def __init__(self):
                self.cancelled = False

            def complete_stream(self, prompt, *, max_new_tokens=512, cancel_token=None, on_chunk=None):
                chunks = ["chunk1", "chunk2", "chunk3", "chunk4"]
                parts = []
                for c in chunks:
                    if cancel_token is not None and cancel_token.is_cancelled:
                        from agent_runtime.cancellation import CancelledError
                        raise CancelledError(cancel_token.reason)
                    parts.append(c)
                return "".join(parts)

            def complete(self, prompt, max_new_tokens=512, prompt_cache_key=""):
                return "<final>done</final>"

        with tempfile.TemporaryDirectory() as tmp:
            ws = WorkspaceContext.build(tmp)
            config = AgentConfig(provider="ollama", max_steps=5)
            client = MockStreamClient()
            agent = Agent(config=config, model_client=client, workspace=ws, cwd=tmp)

            # 设置 cancel token 并在 ask 期间 cancel
            token = CancellationToken()
            agent.cancel_token = token

            # 在另一个线程中延迟 cancel
            import threading
            def delayed_cancel():
                import time
                time.sleep(0.05)
                token.cancel()

            t = threading.Thread(target=delayed_cancel)
            t.start()

            answer = agent.ask("test streaming cancel")
            t.join()
            # cancel 后应返回 cancel 相关结果
            assert "cancel" in answer.lower() or "取消" in answer or "用户" in answer


class TestHardCapContextOverflow:
    """hard_cap 超限 → ContextTooLargeError → stop_reason=context_overflow。"""

    def test_xml_path_hard_cap_overflow_stops_loop(self, config, workspace):
        """XML 路径：hard_cap 超限时 AgentLoop 以 context_overflow 终止。"""
        from agent_runtime.agent_loop import AgentLoop

        config.hard_cap = 100  # 极低硬顶，system+tool+workspace 常规即超
        agent = _make_agent(
            ["<final>should not reach model</final>"],
            config, workspace,
        )
        loop = AgentLoop(agent)
        answer = loop.run("test hard cap overflow")
        assert loop.stop_reason == "context_overflow"
        assert "硬顶" in answer or "超出" in answer or "hard" in answer.lower()

    def test_native_path_hard_cap_overflow_stops_loop(self, config, workspace):
        """Native 路径：hard_cap 超限时 AgentLoop 以 context_overflow 终止。"""
        from agent_runtime.agent_loop import AgentLoop
        from agent_runtime.providers.clients import FakeNativeToolClient

        config.hard_cap = 50  # 极低硬顶
        agent = Agent(
            config=config,
            model_client=FakeNativeToolClient("<final>done</final>"),
            workspace=workspace,
        )
        loop = AgentLoop(agent)
        answer = loop.run("test native hard cap overflow")
        assert loop.stop_reason == "context_overflow"
        assert "硬顶" in answer or "超出" in answer or "hard" in answer.lower()

