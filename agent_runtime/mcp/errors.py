"""MCP 客户端错误（归一化后供 Agent Observation / Trace 使用）。"""

from __future__ import annotations


class McpError(Exception):
    """MCP 调用失败基类。"""

    code: str = "mcp_error"

    def __init__(self, message: str, *, detail: str = "") -> None:
        super().__init__(message)
        self.message = message
        self.detail = detail

    def observation(self) -> str:
        """模型可见的 Observation 文本。"""
        if self.detail:
            return f"Error: [{self.code}] {self.message} ({self.detail})"
        return f"Error: [{self.code}] {self.message}"


class McpTimeoutError(McpError):
    code = "mcp_timeout"


class McpUnavailableError(McpError):
    code = "mcp_unavailable"


class McpSchemaError(McpError):
    code = "mcp_schema_error"
