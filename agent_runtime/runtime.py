"""Agent 运行时：Agent 类骨架 + 模型输出解析。

Agent 是最外层的用户接口，封装了模型客户端、工具注册表、工作区和上下文管理器。
"""

from agent_runtime.config import AgentConfig


class Agent:
    """手写的 LLM Agent。

    封装模型客户端、工具注册、工作区上下文和 prompt 组装。
    """

    def __init__(self, config: AgentConfig, model_client, workspace, tools: dict):
        self.config = config
        self.model_client = model_client
        self.workspace = workspace
        self.tools = tools

    @staticmethod
    def parse(raw: str) -> tuple[str, dict | str]:
        """解析模型输出，提取结构化决策。

        支持两种工具调用格式 + 最终答案：
        - ``<tool>{"name":"x","args":{...}}</tool>`` → (\"tool\", payload_dict)
        - ``<tool name="x" path="f.py"><content>...</content></tool>`` →
          (\"tool\", payload_dict)
        - ``<final>text</final>`` → (\"final\", text)
        - 格式不匹配 → (\"retry\", notice_text)

        Args:
            raw: 模型的原始文本输出。

        Returns:
            (kind, payload) 元组。kind 为 "tool" / "final" / "retry"。
        """
        raw = raw.strip()

        # 尝试匹配 <final>...</final>
        import re

        final_match = re.search(r"<final>(.*?)</final>", raw, re.DOTALL)
        if final_match:
            return ("final", final_match.group(1).strip())

        # 尝试匹配 JSON 格式 <tool>{...}</tool>
        tool_json_match = re.search(r"<tool>\s*(\{.*?\})\s*</tool>", raw, re.DOTALL)
        if tool_json_match:
            import json

            try:
                payload = json.loads(tool_json_match.group(1))
                return ("tool", payload)
            except json.JSONDecodeError:
                return ("retry", "工具调用 JSON 格式无效，请检查后重试。")

        # 尝试匹配 XML 属性格式
        tool_xml_match = re.search(
            r'<tool\s+name="([^"]+)"(.*?)>',
            raw,
            re.DOTALL,
        )
        if tool_xml_match:
            name = tool_xml_match.group(1)
            attrs = tool_xml_match.group(2).strip()
            # 提取 body
            body_match = re.search(r"</tool>(.*)", raw, re.DOTALL)
            body = body_match.group(1).strip() if body_match else ""
            return ("tool", {"name": name, "attrs": attrs, "body": body})

        # 都不匹配
        notice = (
            "无法解析模型输出，请使用 <tool>JSON</tool> "
            f"或 <final>text</final> 格式。\n收到: {raw[:200]}"
        )
        return ("retry", notice)
