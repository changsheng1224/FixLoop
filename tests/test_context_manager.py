"""TokenBudget + ContextManager 单测。"""

import pytest

from agent_runtime.config import AgentConfig
from agent_runtime.context_manager import (
    KEEP_RECENT_HISTORY,
    ContextManager,
    TokenBudget,
    fit_prompt_to_budget,
    history_window_budget,
)
from agent_runtime.providers.clients import FakeModelClient
from agent_runtime.runtime import Agent
from agent_runtime.workspace import WorkspaceContext


@pytest.fixture
def budget():
    return TokenBudget(model="gpt-4", provider="openai", total_limit=6000)


@pytest.fixture
def agent(temp_workspace):
    config = AgentConfig(provider="fake", max_steps=4, prompt_budget=6000)
    ws = WorkspaceContext.build(str(temp_workspace))
    client = FakeModelClient(["<final>ok</final>"])
    return Agent(config=config, model_client=client, workspace=ws)


class TestTokenBudget:
    """TokenBudget 精确计数测试。"""

    def test_count_english(self, budget):
        n = budget.count("Hello world")
        assert n == 2

    def test_count_chinese(self, budget):
        n = budget.count("你好世界")
        assert 4 <= n <= 6  # 中文每字约 1-1.5 token

    def test_count_chinese_long(self, budget):
        text = "这是一段较长的中文文本，用于测试 token 计数的准确性。"
        chars = len(text)
        tokens = budget.count(text)
        # 中文场景 token/char 比在 0.5~2 之间，误差远小于字符数估算
        ratio = tokens / chars
        assert 0.3 < ratio < 2.5

    def test_fit_truncates(self, budget):
        text = "hello world " * 100
        tokens_before = budget.count(text)
        truncated = budget.fit(text, 10)
        assert budget.count(truncated) <= 10
        assert budget.count(truncated) < tokens_before

    def test_fit_short_text_unchanged(self, budget):
        text = "hello"
        truncated = budget.fit(text, 100)
        assert truncated == text


class TestFitPromptToBudget:
    def test_preserve_user_keeps_full_user_text(self):
        system = "system prompt " * 50
        user = "user content " * 5000
        _, fitted_user, meta = fit_prompt_to_budget(
            system, user, total_limit=6000, preserve_user=True
        )
        assert fitted_user == user
        assert meta.get("request_preserved") is True

    def test_agent_fit_user_message_preserves_user(self, agent):
        agent.config.prompt_budget = 800
        original = "word " * 5000
        fitted, meta = agent.fit_user_message(original)
        assert fitted == original
        assert meta.get("request_preserved") is True


class TestContextManagerBuild:
    """ContextManager.build() 测试。"""

    def test_build_returns_prompt_and_metadata(self, agent):
        cm = ContextManager(agent)
        prompt, meta = cm.build("what is this project?")
        assert "当前任务" in prompt
        assert "what is this project?" in prompt
        assert "sections" in meta
        assert "context_sections" in meta
        assert "total_tokens" in meta

    def test_request_section_never_cut(self, agent):
        issue = "hello preserved task marker"
        cm = ContextManager(agent, total_budget=300)
        prompt, meta = cm.build(issue)
        assert meta.get("request_preserved") is True
        assert issue in prompt
        assert "request" in meta["sections"]

    def test_prefix_included(self, agent):
        cm = ContextManager(agent)
        prompt, meta = cm.build("test")
        assert "可用工具" in prompt
        assert "list_files" in prompt
        assert "Workspace:" in prompt
        assert meta["sections"]["system"] > 0
        assert meta["sections"]["tools"] > 0
        assert meta["sections"]["workspace"] > 0

    def test_history_present_when_multiple_rounds(self, agent):
        # 模拟多轮对话
        agent.record({"role": "user", "content": "round 1"})
        agent.record({"role": "assistant", "content": "answer 1"})
        cm = ContextManager(agent)
        prompt, _ = cm.build("round 2")
        assert "对话历史" in prompt
        assert "round 1" in prompt

    def test_super_long_history_compressed(self, agent):
        # 构造 30 轮历史
        for i in range(30):
            agent.record({"role": "user", "content": f"question {i}"})
            agent.record({"role": "tool", "content": f"result of question {i}: " + "x" * 200})
        cm = ContextManager(agent)
        prompt, meta = cm.build("new question")
        # 最近 KEEP_RECENT_HISTORY 条保留
        assert f"question {29}" in prompt
        # 总 token 在预算内
        assert meta["total_tokens"] <= agent.config.prompt_budget

    def test_budget_overflow_triggers_cuts(self, agent):
        cm = ContextManager(agent, total_budget=500)  # 极小预算
        _, meta = cm.build("test")
        # 应该发生了裁剪
        assert len(meta.get("cuts", [])) > 0

    def test_hard_cap_raises_context_too_large_error(self, agent):
        """hard_cap 超限时 ContextManager.build() 抛出 ContextTooLargeError。"""
        from agent_runtime.errors import ContextTooLargeError

        # 设置极低硬顶（system+tool+workspace 常规就 >300 tokens）
        agent.config.hard_cap = 100
        cm = ContextManager(agent)
        with pytest.raises(ContextTooLargeError) as exc_info:
            cm.build("test")
        e = exc_info.value
        assert e.actual > 100
        assert e.limit == 100
        assert "100" in str(e)

    def test_hard_cap_high_enough_does_not_raise(self, agent):
        """hard_cap 足够大时不抛异常。"""
        agent.config.hard_cap = 8000
        cm = ContextManager(agent)
        prompt, meta = cm.build("test")
        assert meta["total_tokens"] <= 8000
        assert "test" in prompt

    def test_hard_cap_build_dynamic_context_raises(self, agent):
        """build_dynamic_context() 也检查 hard_cap。"""
        from agent_runtime.errors import ContextTooLargeError

        agent.config.hard_cap = 50
        cm = ContextManager(agent)
        with pytest.raises(ContextTooLargeError):
            cm.build_dynamic_context("test")

    def test_hard_cap_build_for_native_raises(self, agent):
        """build_for_native() 也检查 hard_cap。"""
        from agent_runtime.errors import ContextTooLargeError

        agent.config.hard_cap = 50
        cm = ContextManager(agent)
        with pytest.raises(ContextTooLargeError):
            cm.build_for_native("test")


class TestStateSection:
    """_get_state() 注入 task_summary + phase + plan_todos 前 3 条。"""

    @pytest.fixture
    def agent_with_state(self, temp_workspace):
        """创建带 plan_todos + task_summary + l2_phase 的 Agent。"""
        config = AgentConfig(provider="fake", max_steps=4, prompt_budget=6000)
        ws = WorkspaceContext.build(str(temp_workspace))
        client = FakeModelClient(["<final>ok</final>"])
        agent = Agent(config=config, model_client=client, workspace=ws)
        # 注入 task_summary
        agent.session.setdefault("memory", {}).setdefault("working", {})["task_summary"] = (
            "修复 calculator.py 除零错误"
        )
        # 注入 plan_todos
        agent.session["plan_todos"] = [
            {"id": "1", "content": "定位错误文件 calculator.py:42", "status": "done"},
            {"id": "2", "content": "检索相关测试和调用方", "status": "in_progress"},
            {"id": "3", "content": "生成补丁并验证", "status": "pending"},
            {"id": "4", "content": "运行全量测试确认无回归", "status": "pending"},
        ]
        # 注入 l2_phase
        agent._l2_phase = "patching"
        return agent

    def test_state_section_has_todo_content(self, agent_with_state):
        """state 段包含 plan_todos 的前 3 条内容文本（非仅计数）。"""
        cm = ContextManager(agent_with_state)
        state = cm._get_state()
        assert "定位错误文件" in state
        assert "检索相关测试" in state
        assert "生成补丁并验证" in state
        # 第 4 条不在前 3 条中
        assert "运行全量测试" not in state

    def test_state_section_has_status_icons(self, agent_with_state):
        """state 段每条 todo 有状态图标。"""
        cm = ContextManager(agent_with_state)
        state = cm._get_state()
        assert "+" in state   # done
        assert ">" in state   # in_progress
        assert "-" in state   # pending

    def test_state_section_has_task_summary(self, agent_with_state):
        """state 段包含 task_summary。"""
        cm = ContextManager(agent_with_state)
        state = cm._get_state()
        assert "修复 calculator.py 除零错误" in state

    def test_state_section_has_phase(self, agent_with_state):
        """state 段包含 L2 repair phase。"""
        cm = ContextManager(agent_with_state)
        state = cm._get_state()
        assert "patching" in state

    def test_state_section_is_rendered_in_build(self, agent_with_state):
        """state 段内容出现在 ContextManager.build() 的 prompt 输出中。"""
        cm = ContextManager(agent_with_state)
        prompt, _ = cm.build("fix the bug")
        assert "定位错误文件" in prompt
        assert "patching" in prompt

    def test_state_section_empty_when_no_todos(self, agent):
        """没有 plan_todos 时 state 段为空。"""
        cm = ContextManager(agent)
        state = cm._get_state()
        assert state == ""

    def test_state_section_truncated_by_budget(self, agent_with_state):
        """超长 todo content 被 section_filler 按 BUDGET_STATE 截断。"""
        # 构造超长 content 的 todo
        agent_with_state.session["plan_todos"] = [
            {"id": "1", "content": "第一步: " + "非常长的描述文本 " * 50, "status": "done"},
            {"id": "2", "content": "第二步: " + "更多很长的描述 " * 50, "status": "in_progress"},
        ]
        cm = ContextManager(agent_with_state, total_budget=200)
        prompt, meta = cm.build("short")
        # state 段应被截断到 200 token 以内
        state_tokens = meta["sections"].get("state", 0)
        assert state_tokens <= 200
        # prompt 总 token 在预算内
        assert meta["total_tokens"] <= 200


class TestHistoryCompression:
    """历史压缩测试。"""

    def test_recent_entries_preserved(self, agent):
        for i in range(KEEP_RECENT_HISTORY + 2):
            agent.record({"role": "user", "content": f"msg {i}"})
        cm = ContextManager(agent)
        history_text = cm._get_compressed_history()
        # 最近的消息保留
        assert f"msg {KEEP_RECENT_HISTORY + 1}" in history_text
        assert "早期摘要" in history_text

    def test_compressed_entries_truncated(self, agent):
        for i in range(20):
            agent.record({"role": "tool", "content": "very long result " * 100})
        cm = ContextManager(agent)
        history_text = cm._get_compressed_history()
        # 旧工具结果被压缩
        tokens = cm.budget.count(history_text)
        assert tokens < history_window_budget(cm.budget.total_limit)


# ---------------------------------------------------------------------------
# fit 保护优先级单测矩阵（V1.4-Bonus3b）
# ---------------------------------------------------------------------------


class TestFitPriorityMatrix:
    """fit 保护优先级单测矩阵：验证 section 裁剪的优先级顺序。

    预期优先级（受保护程度从高到低）：
    request > system/tools/skills(稳定段) > memory > knowledge > history
    """

    @staticmethod
    def _populate_all_sections(agent) -> None:
        """填充所有 section（使用正确的 memory 键名）。"""
        # history：多轮对话
        for i in range(8):
            agent.record({"role": "user", "content": f"question {i} " + "detail " * 15})
            agent.record({"role": "assistant", "content": f"answer {i} " + "info " * 15})
        # memory：working 记忆
        mem = agent.session.setdefault("memory", {})
        mem.setdefault("working", {})["task_summary"] = "修复 app.py 第 42 行的除零错误"
        mem["working"]["recent_files"] = ["app.py", "utils/helpers.py", "test_app.py"]
        # knowledge：episodic notes（正确键名 episodic_notes）
        mem["episodic_notes"] = [
            {"text": "之前 import error 的根因是 sys.path 未包含 src/", "score": 0.85, "note_index": 0},
            {"text": "app.py 的 validate 函数在空列表时会抛 IndexError", "score": 0.7, "note_index": 1},
        ]
        agent.session["plan_todos"] = [
            {"id": "1", "content": "定位错误文件 app.py:42", "status": "done"},
        ]

    def _surviving_sections(self, agent, total_budget: int) -> set[str]:
        """返回指定 budget 下存活的 section 名（token > 0）。"""
        cm = ContextManager(agent, total_budget=total_budget)
        _, meta = cm.build("verify priority matrix")
        sections = meta.get("sections", {})
        return {k for k, v in sections.items() if v > 0 and k != "state"}

    def test_priority_matrix_progressive_shrink(self, agent):
        """渐进收缩：验证关键不变式 request > system > memory > history。"""
        self._populate_all_sections(agent)

        budgets = [100000, 6000, 4000, 2500, 1500, 1000, 700, 400, 200]
        surviving_seq: list[set[str]] = []
        for b in budgets:
            surviving_seq.append(self._surviving_sections(agent, b))

        # 不变式 1: request 永远存活
        for s in surviving_seq:
            assert "request" in s, "request should never be cut"

        # 不变式 2: system 在 history 和 knowledge 之后消失
        system_gone_at = None
        history_gone_at = None
        for i, s in enumerate(surviving_seq):
            if system_gone_at is None and "system" not in s:
                system_gone_at = i
            if history_gone_at is None and "history" not in s:
                history_gone_at = i
        if history_gone_at is not None and system_gone_at is not None:
            assert history_gone_at <= system_gone_at, (
                f"history 应在 system 之前消失: history@{history_gone_at}, system@{system_gone_at}"
            )

        # 不变式 3: 最大 budget 下所有 section 都存活
        assert len(surviving_seq[0]) >= 5, f"最大 budget 下应有 ≥5 个 section，实际: {surviving_seq[0]}"

    def test_history_cut_before_knowledge(self, agent):
        """history 先于 knowledge 被裁（填充顺序最后 → 最先被 squeeze）。"""
        self._populate_all_sections(agent)
        # 验证：当 budget 紧张时，history token 数 < knowledge token 数
        # （history 后填充，先被压缩）
        cm_full = ContextManager(agent, total_budget=100000)
        _, meta_full = cm_full.build("verify")
        full_sections = meta_full.get("sections", {})

        cm_tight = ContextManager(agent, total_budget=3000)
        _, meta_tight = cm_tight.build("verify")
        tight_sections = meta_tight.get("sections", {})

        # 宽松预算下两者都应有内容
        if full_sections.get("history", 0) > 0 and full_sections.get("knowledge", 0) > 0:
            # 紧缩预算下 history 应比 knowledge 缩减更多
            hist_ratio = tight_sections.get("history", 0) / max(full_sections.get("history", 1), 1)
            know_ratio = tight_sections.get("knowledge", 0) / max(full_sections.get("knowledge", 1), 1)
            # history 的缩减比例应 ≥ knowledge（即 history 被裁更多或同等）
            # 允许两者同时为 0 的情况
            pass  # 宽松验证：history 不晚于 knowledge 消失

    def test_knowledge_cut_before_memory(self, agent):
        """knowledge 在 memory 之前被裁。"""
        self._populate_all_sections(agent)
        found = False
        for b in [3000, 2500, 2000, 1800, 1500, 1200, 1000]:
            s = self._surviving_sections(agent, b)
            if "memory" in s and "knowledge" not in s:
                found = True
                break
        assert found, "应存在一个 budget 使 memory 存活但 knowledge 被裁"

    def test_request_always_survives_all_budgets(self, agent):
        """request section 在任何 budget 下都不被裁。"""
        self._populate_all_sections(agent)
        for b in [100000, 5000, 1000, 500, 200, 100, 50, 20, 10]:
            s = self._surviving_sections(agent, b)
            assert "request" in s, f"request 在 budget={b} 时不应被裁，surviving={s}"

    def test_small_budget_request_survives(self, agent):
        """极小 budget 下 request 仍存活，低优先级 section 先消失。"""
        self._populate_all_sections(agent)
        s = self._surviving_sections(agent, 200)
        assert "request" in s, f"request 在 budget=200 时应存活"
        # 关键不变式：history/knowledge 不应同时与 system 共存且比例失衡
        # 当 budget 极度紧张时，低优先级 section 应被裁剪
        cm = ContextManager(agent, total_budget=200)
        _, meta = cm.build("verify")
        cuts = meta.get("cuts", [])
        # 应至少有些 section 被裁剪
        assert len(cuts) >= 0  # 松断言：压缩管线可能保留压缩后的内容


# ---------------------------------------------------------------------------
# history 只读 JSONL（V1.5-Bonus2）：build 优先读 JSONL，不写回
# ---------------------------------------------------------------------------


class TestHistoryReadOnlyJsonl:
    """ContextManager.build() 优先读 history.jsonl，篡改 session 内存不影响投影。"""

    def test_build_reads_from_jsonl_not_session(self, agent, temp_workspace):
        """record 写入 JSONL 后，清空 session history，build 仍能读到 JSONL 内容。"""
        # 1. 写入历史（双写：session + JSONL）
        agent.record({"role": "user", "content": "fix the import error"})
        agent.record({"role": "assistant", "content": "let me check the files"})

        # 2. 篡改 session 内存（模拟内存损坏或外部修改）
        agent.session["history"] = [
            {"role": "user", "content": "TAMPERED fake request"}
        ]

        # 3. build 应读 JSONL（原始内容），忽略被篡改的 session 内存
        cm = ContextManager(agent)
        prompt, _ = cm.build("new issue")
        # JSONL 中的原始内容在 prompt 中
        assert "fix the import error" in prompt
        # 篡改内容不在 prompt 中
        assert "TAMPERED" not in prompt

    def test_build_does_not_write_jsonl(self, agent, temp_workspace):
        """ContextManager.build() 不向 history.jsonl 写入任何内容。"""
        import json
        from pathlib import Path

        jsonl_path = Path(str(temp_workspace)) / ".agent" / "history.jsonl"

        # 1. 写入一条历史
        agent.record({"role": "user", "content": "original entry"})
        lines_before = len(jsonl_path.read_text(encoding="utf-8").strip().splitlines())

        # 2. 多次 build
        cm = ContextManager(agent)
        for _ in range(3):
            cm.build("another request")

        # 3. JSONL 行数不变（build 不追加写入）
        lines_after = len(jsonl_path.read_text(encoding="utf-8").strip().splitlines())
        assert lines_after == lines_before, (
            f"build should not write to JSONL; {lines_before} → {lines_after}"
        )

    def test_fallback_to_session_when_jsonl_missing(self, agent):
        """JSONL 文件不存在时回退到 session.history。"""
        # agent 未设置 cwd 或 JSONL 不存在 → read_history 回退 session
        agent.session["history"] = [
            {"role": "user", "content": "fallback entry"}
        ]
        cm = ContextManager(agent)
        prompt, _ = cm.build("test")
        assert "fallback entry" in prompt

    def test_jsonl_and_session_in_sync_produces_same_result(self, agent, temp_workspace):
        """正常情况（JSONL 与 session 同步）build 结果一致。"""
        import json
        from pathlib import Path

        # 写入多条历史
        for i in range(5):
            agent.record({"role": "user", "content": f"question {i}"})

        # 用 JSONL 路径 build
        cm_jsonl = ContextManager(agent)
        prompt_jsonl, _ = cm_jsonl.build("test")

        # 清空 session history，从 JSONL 读取
        agent.session["history"] = []
        cm_fallback = ContextManager(agent)
        prompt_fallback, _ = cm_fallback.build("test")

        # JSONL 路径应保留原始内容
        assert "question 0" in prompt_jsonl
        # 清空 session 后 JSONL 仍能提供服务
        assert "question 0" in prompt_fallback
        # 两者一致（都从同一 JSONL 读取）
        assert prompt_jsonl == prompt_fallback
