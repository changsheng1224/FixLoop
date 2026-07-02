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
        light_client=None,
        dry_run: bool = False,
        tools: dict | None = None,
        system_prompt: str = "",
    ):
        self.config = config
        self.model_client = model_client
        self.light_client = light_client
        self.dry_run = dry_run
        self.workspace = workspace
        self._cwd = cwd or workspace.repo_root or str(Path.cwd())

        # 构建工具上下文和注册表（允许外部注入）
        self.tool_context = ToolContext(root=self._cwd)
        self.tools = tools if tools is not None else build_tool_registry(self.tool_context)
        self._tool_names = set(self.tools.keys())

        # 会话状态 + 记忆
        self.session: dict = self._new_session()

        # 缓存 prefix（工具不变时复用，支持自定义 prompt）
        self._prefix = self._build_prefix(system_prompt)

        # M4 模块：配额 + 熔断 + 语义记忆
        import sys as _sys

        from agent_runtime.providers.circuit_breaker import CircuitBreaker
        from agent_runtime.tool_executor import QuotaEnforcer

        print("[agent_runtime] 加载语义模型 (~90MB)...",
              file=_sys.stderr, end="", flush=True)
        from agent_runtime.features.memory import SemanticMemory
        self.semantic_memory = SemanticMemory()
        if self.semantic_memory.available:
            print(" ✅", file=_sys.stderr)
        else:
            print(" ⚠ 不可用（语义检索降级为 keywords 模式）", file=_sys.stderr)

        self.circuit_breaker = CircuitBreaker()
        self.quota = QuotaEnforcer()

    # ---- 公开方法 ----

    def ask(self, user_message: str, callback=None) -> str:
        """执行一次用户请求，返回最终答案。

        Args:
            user_message: 用户输入。
            callback: 可选的 ProgressCallback 实例。

        Returns:
            模型返回的最终答案文本。
        """
        from agent_runtime.agent_loop import AgentLoop

        loop = AgentLoop(agent=self)
        return loop.run(user_message, callback=callback)

    def prompt(self, user_message: str) -> str:
        """组装完整 prompt 文本（经 ContextManager token 预算控制）。"""
        prompt_text, _ = self._build_prompt_with_meta(user_message)
        return prompt_text

    def _build_prompt_with_meta(self, user_message: str) -> tuple[str, dict]:
        """同 prompt()，但返回 (text, metadata)。"""
        from agent_runtime.context_manager import ContextManager

        cm = ContextManager(self)
        return cm.build(user_message)

    def record(self, item: dict):
        """向会话历史追加一条记录。

        Args:
            item: 包含 role 和 content 的字典。
        """
        self.session["history"].append(item)

    def execute_tool(self, name: str, args: dict):
        """执行指定工具（经 ToolExecutor 闸口），返回 ToolExecutionResult。"""
        from agent_runtime.tool_executor import ToolExecutor

        if not hasattr(self, "_tool_executor"):
            self._tool_executor = ToolExecutor(
                agent=self,
                approval_policy=self.config.approval,
                dry_run=self.dry_run,
                quota=self.quota,
            )
        return self._tool_executor.execute(name, args)

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

        # 尝试匹配 JSON 函数调用格式：{"action":"tool_name","arguments":{...}}
        # DeepSeek 有时会输出这种 OpenAI 风格的 JSON
        if raw.startswith("{"):
            try:
                data = json.loads(raw)
                if isinstance(data, dict) and any(k in data for k in ("action", "name", "tool", "function")):
                    tool_name = data.get("action") or data.get("name") or data.get("tool") or data.get("function")
                    tool_args = data.get("arguments") or data.get("args") or data.get("parameters") or {}
                    if isinstance(tool_name, str) and tool_name:
                        return ("tool", {"name": tool_name, "args": tool_args})
            except json.JSONDecodeError:
                pass

        # 模型直接输出了 JSON 答案（如 SuspectList），视为 final
        if (raw.startswith("[") or raw.startswith("{")) and not raw.startswith("<tool"):
            try:
                json.loads(raw)  # 验证是合法 JSON
            except json.JSONDecodeError:
                pass
            else:
                return ("final", raw)

        # 模型用 markdown 代码块包裹 JSON（支持嵌套括号）
        md_start = re.search(r"```(?:json)?\s*", raw)
        if md_start:
            content = raw[md_start.end():]
            md_end = content.rfind("```")
            if md_end >= 0:
                inner = content[:md_end].strip()
                if (inner.startswith("{") or inner.startswith("[")):
                    return ("final", inner)

        # 尝试匹配 DeepSeek 原生 <function_calls> 格式
        # <function_calls>
        # <invoke name="tool_name">
        # <parameter name="param1">value1</parameter>
        # </invoke>
        # </function_calls>
        fc_match = re.search(
            r"<function_calls>\s*(.*?)\s*</function_calls>",
            raw, re.DOTALL,
        )
        if fc_match:
            inner = fc_match.group(1)
            invokes = re.findall(
                r"<invoke\s+name=\"(\w+)\">(.*?)</invoke>",
                inner, re.DOTALL,
            )
            if invokes:
                name = invokes[0][0]  # 取第一个 invoke（每次只调一个工具）
                params_str = invokes[0][1]
                args = {}
                for param_m in re.finditer(
                    r'<parameter\s+name="(\w+)">(.*?)</parameter>',
                    params_str, re.DOTALL,
                ):
                    args[param_m.group(1)] = param_m.group(2).strip()
                return ("tool", {"name": name, "args": args})

        # 尝试匹配 JSON 格式 <tool>{...}</tool>（支持嵌套 JSON）
        json_match = _extract_json_between_tags(raw, "<tool>", "</tool>")
        if json_match:
            try:
                payload = json.loads(json_match)
                return ("tool", payload)
            except json.JSONDecodeError:
                return ("retry",
                    "工具调用 JSON 格式无效。<tool> 内必须是合法 JSON：\n"
                    '  {"name":"工具名","args":{"参数":"值"}}\n'
                    "请检查引号、括号是否匹配后重试。")

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
        # 检测模型是否使用了错误的 XML 格式（如 <read_file>...</read_file>）
        wrong_xml = re.match(r"<\w+>", raw)
        if wrong_xml:
            tag = wrong_xml.group(0).strip("<>")
            notice = (
                f"格式错误：你使用了 <{tag}>...</{tag}> 格式，这是不支持的。\n"
                "唯一正确的工具调用格式是：\n"
                f'<tool>{{"name":"{tag}","args":{{"path":"文件路径"}}}}</tool>\n'
                "请用 <tool> 包裹 JSON 的格式重新调用。"
            )
        else:
            notice = (
                "无法解析你的输出。请严格使用以下格式之一：\n"
                '  调用工具: <tool>{"name":"工具名","args":{...}}</tool>\n'
                "  返回答案: <final>你的答案</final>\n"
                f"收到: {raw[:200]}"
            )
        return ("retry", notice)

    # ---- 内部方法 ----

    def _build_prefix(self, system_prompt: str = ""):
        """构建 System Prompt 前缀。system_prompt 非空时用它替代默认前缀。"""
        if system_prompt:
            from agent_runtime.prompt_prefix import PromptPrefix
            text = system_prompt + "\n\n" + self.workspace.text()
            return PromptPrefix(
                text=text,
                hash="",
                workspace_fingerprint=self.workspace.fingerprint(),
                tool_signature="",
            )
        return build_prompt_prefix(
            self.workspace, self.tools,
            dry_run=self.dry_run,
            approval=self.config.approval,
        )

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
