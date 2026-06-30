"""Agent 运行时：Agent 类 + 模型输出解析 + ask() 入口。

Agent 是最外层的用户接口，封装了模型客户端、工具注册表、工作区和上下文管理器。
"""

import json
import re
from pathlib import Path

from agent_runtime.config import AgentConfig
from agent_runtime.prompt_prefix import build_prompt_prefix
from agent_runtime.tool_context import ToolContext
from agent_runtime.tools import build_tool_registry


class Agent:
    """手写的 LLM Agent。

    封装模型客户端、工具注册、工作区上下文和 prompt 组装。
    """

    def __init__(
        self,
        config: AgentConfig,
        model_client,
        workspace,
        cwd: str | None = None,
    ):
        self.config = config
        self.model_client = model_client
        self.workspace = workspace
        self._cwd = cwd or workspace.repo_root or str(Path.cwd())

        # 构建工具上下文和注册表
        self.tool_context = ToolContext(root=self._cwd)
        self.tools = build_tool_registry(self.tool_context)
        self._tool_names = set(self.tools.keys())

        # 会话状态 + 记忆
        self.session: dict = self._new_session()

        # 缓存 prefix（工具不变时复用）
        self._prefix = self._build_prefix()

        # M4 模块：配额 + 熔断 + 语义记忆
        import sys as _sys
        from agent_runtime.providers.circuit_breaker import CircuitBreaker
        from agent_runtime.tool_executor import QuotaEnforcer

        print("[agent_runtime] 加载语义模型 (all-MiniLM-L6-v2, ~90MB)...", file=_sys.stderr, end="", flush=True)
        from agent_runtime.features.memory import SemanticMemory
        self.semantic_memory = SemanticMemory()
        if self.semantic_memory.available:
            print(" ✅", file=_sys.stderr)
        else:
            print(" ⚠ 不可用（语义检索降级为 keywords 模式）", file=_sys.stderr)

        self.circuit_breaker = CircuitBreaker()
        self.quota = QuotaEnforcer()

    # ---- 公开方法 ----

    def ask(self, user_message: str) -> str:
        """执行一次用户请求，返回最终答案。

        这是 Agent 的核心入口方法。

        Args:
            user_message: 用户输入。

        Returns:
            模型返回的最终答案文本。
        """
        from agent_runtime.agent_loop import AgentLoop

        loop = AgentLoop(agent=self)
        return loop.run(user_message)

    def prompt(self, user_message: str) -> str:
        """组装完整 prompt 文本（经 ContextManager token 预算控制）。

        Args:
            user_message: 当前用户输入。

        Returns:
            完整的 prompt 文本。
        """
        from agent_runtime.context_manager import ContextManager

        cm = ContextManager(self)
        prompt_text, _metadata = cm.build(user_message)
        return prompt_text

    def record(self, item: dict):
        """向会话历史追加一条记录。

        Args:
            item: 包含 role 和 content 的字典。
        """
        self.session["history"].append(item)

    def execute_tool(self, name: str, args: dict):
        """执行指定工具（经 ToolExecutor 闸口），返回 ToolExecutionResult。

        Args:
            name: 工具名称。
            args: 工具参数字典。

        Returns:
            ToolExecutionResult 实例。
        """
        from agent_runtime.tool_executor import ToolExecutor

        executor = ToolExecutor(
            agent=self,
            approval_policy=self.config.approval,
            dry_run=getattr(self, "_dry_run", False),
        )
        return executor.execute(name, args)

    def is_tool_available(self, name: str) -> bool:
        """检查工具是否在注册表中。"""
        return name in self._tool_names

    # ---- 类方法 ----

    @classmethod
    def from_session(
        cls,
        model_client,
        workspace,
        session_store,
        session_id: str,
        **kwargs,
    ):
        """从持久化的 session 恢复 Agent 实例。

        Args:
            model_client: 模型客户端。
            workspace: WorkspaceContext。
            session_store: SessionStore 实例。
            session_id: 要恢复的 session id。
            **kwargs: 传递给 __init__ 的额外参数。

        Returns:
            恢复的 Agent 实例，如果 session 不存在则返回 None。
        """
        session = session_store.load(session_id)
        if session is None:
            return None

        config = kwargs.pop("config", None)
        if config is None:
            from agent_runtime.config import AgentConfig
            config = AgentConfig()

        cwd = kwargs.pop("cwd", None) or workspace.repo_root

        # 创建 Agent（先不注入 session）
        agent = cls(
            config=config,
            model_client=model_client,
            workspace=workspace,
            cwd=cwd,
            **kwargs,
        )

        # 恢复 session 数据
        agent.session = session
        # 恢复 memory 状态
        from agent_runtime.features.memory import default_memory_state
        agent.session.setdefault("memory", default_memory_state())
        agent.session.setdefault("checkpoints", [])

        return agent

    # ---- 静态方法 ----

    @staticmethod
    def parse(raw: str) -> tuple[str, dict | str]:
        """解析模型输出，提取结构化决策。

        支持两种工具调用格式 + 最终答案：
        - ``<tool>{"name":"x","args":{...}}</tool>`` → (\"tool\", payload_dict)
        - ``<tool name="x" path="f.py">body</tool>`` → (\"tool\", payload_dict)
        - ``<final>text</final>`` → (\"final\", text)
        - 格式不匹配 → (\"retry\", notice_text)

        Args:
            raw: 模型的原始文本输出。

        Returns:
            (kind, payload) 元组。kind 为 \"tool\" / \"final\" / \"retry\"。
        """
        raw = raw.strip()

        # 尝试匹配 <final>...</final>
        final_match = re.search(r"<final>(.*?)</final>", raw, re.DOTALL)
        if final_match:
            return ("final", final_match.group(1).strip())

        # 尝试匹配 JSON 格式 <tool>{...}</tool>（支持嵌套 JSON）
        json_match = _extract_json_between_tags(raw, "<tool>", "</tool>")
        if json_match:
            try:
                payload = json.loads(json_match)
                return ("tool", payload)
            except json.JSONDecodeError:
                return ("retry", "工具调用 JSON 格式无效，请检查后重试。")

        # 尝试匹配 XML 属性格式：<tool name="x" ...>body</tool>
        tool_xml_match = re.search(
            r'<tool\s+name="([^"]+)"(.*?)>(.*?)</tool>',
            raw,
            re.DOTALL,
        )
        if tool_xml_match:
            name = tool_xml_match.group(1)
            attrs = tool_xml_match.group(2).strip()
            body = tool_xml_match.group(3).strip()
            return ("tool", {"name": name, "attrs": attrs, "body": body})

        # 都不匹配
        notice = (
            "无法解析模型输出，请使用 <tool>JSON</tool> "
            f"或 <final>text</final> 格式。\n收到: {raw[:200]}"
        )
        return ("retry", notice)

    # ---- 内部方法 ----

    def _build_prefix(self):
        """构建 System Prompt 前缀（缓存）。"""
        return build_prompt_prefix(self.workspace, self.tools)

    def _new_session_id(self) -> str:
        """生成新的会话 ID。"""
        import uuid
        return str(uuid.uuid4())[:8]

    def _new_session(self) -> dict:
        """创建新会话（含记忆状态）。"""
        from agent_runtime.features.memory import default_memory_state

        return {
            "id": self._new_session_id(),
            "history": [],
            "memory": default_memory_state(),
        }

    def update_memory_after_tool(self, name: str, args: dict, result_text: str):
        """工具执行后更新记忆（Working + Episodic）。

        由 AgentLoop 在每个工具执行成功后调用。
        """
        from agent_runtime.features.memory import (
            append_note,
            invalidate_file_summary,
            remember_file,
            set_file_summary,
        )

        mem = self.session["memory"]
        path = args.get("path", "")

        if name == "read_file" and path:
            remember_file(mem, path)
            # 从结果中取前 180 字符作为摘要
            summary = result_text[:180]
            set_file_summary(mem, path, summary)

        elif name in ("write_file", "patch_file") and path:
            remember_file(mem, path)
            invalidate_file_summary(mem, path)

        elif name == "run_shell":
            command = args.get("command", "")
            if "Error" in result_text or "exit_code: 1" in result_text:
                append_note(
                    mem,
                    f"Shell 命令失败: {command[:100]} — {result_text[:100]}",
                    tags=["shell", "error"],
                    source=command[:80],
                    kind="error",
                )
            else:
                append_note(
                    mem,
                    f"Shell 命令成功: {command[:100]}",
                    tags=["shell"],
                    source=command[:80],
                    kind="observation",
                )

        elif name == "search":
            pattern = args.get("pattern", "")
            if pattern:
                append_note(
                    mem,
                    f"搜索 '{pattern}': {result_text[:150]}",
                    tags=["search"],
                    source=pattern,
                    kind="observation",
                )

    def _format_history(self) -> str:
        """将会话历史格式化为 prompt 文本。"""
        lines = ["## 对话历史", ""]
        for item in self.session["history"]:
            role = item.get("role", "unknown")
            content = str(item.get("content", ""))
            # 截断过长的工具输出
            if role == "tool" and len(content) > 500:
                content = content[:500] + f"\n... (截断，共 {len(content)} 字符)"
            if role == "user" and len(content) > 300:
                content = content[:300] + "..."
            lines.append(f"**{role}**: {content}")
            lines.append("")
        return "\n".join(lines)


def _extract_json_between_tags(text: str, open_tag: str, close_tag: str) -> str:
    """从标签之间提取 JSON 文本（正确处理嵌套大括号）。

    Args:
        text: 原始文本。
        open_tag: 开标签。
        close_tag: 闭标签。

    Returns:
        标签之间的文本，未找到返回空字符串。
    """
    start = text.find(open_tag)
    if start == -1:
        return ""
    start += len(open_tag)
    end = text.find(close_tag, start)
    if end == -1:
        return ""
    return text[start:end].strip()
