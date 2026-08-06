"""MCP 客户端错误（归一化后供 Agent Observation / Trace 使用）。"""

from __future__ import annotations

from agent_runtime.canonical_protocol import ToolErrorCode, decide_tool_error


class McpError(Exception):
    """MCP 调用失败基类。"""

    code: str = "mcp_error"
    canonical_code: ToolErrorCode = ToolErrorCode.TOOL_EXECUTION_FAILED

    def __init__(self, message: str, *, detail: str = "") -> None:
        super().__init__(message)
        self.message = message
        self.detail = detail

    def observation(self) -> str:
        """模型可见的 Observation 文本。"""
        if self.detail:
            return f"Error: [{self.code}] {self.message} ({self.detail})"
        return f"Error: [{self.code}] {self.message}"

    def metadata(self) -> dict:
        decision = decide_tool_error(self.canonical_code.value)
        return {
            "tool_status": "error",
            "tool_error_code": self.canonical_code.value,
            "mcp_error_code": self.code,
            "retryable": decision.retryable,
            "retry_limit": decision.retry_limit,
            "model_hint": decision.model_hint,
            "error_detail": self.detail,
        }


class McpTimeoutError(McpError):
    code = "mcp_timeout"
    canonical_code = ToolErrorCode.TOOL_TIMEOUT


class McpUnavailableError(McpError):
    code = "mcp_unavailable"
    canonical_code = ToolErrorCode.MCP_UNAVAILABLE


class McpSchemaError(McpError):
    code = "mcp_schema_error"
    canonical_code = ToolErrorCode.INVALID_ARGUMENTS
