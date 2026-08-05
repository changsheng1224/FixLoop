"""终态工具：执行成功后结束 AgentLoop，并以工具输出作为 final answer。"""

from __future__ import annotations


class TerminalToolAccepted(Exception):
    """工具带 ``terminal=True`` 且执行成功时由 AgentLoop 抛出，供 native/XML 路径短路。"""

    def __init__(self, payload: str, *, tool_name: str = ""):
        super().__init__(tool_name or "terminal_tool")
        self.payload = payload
        self.tool_name = tool_name
