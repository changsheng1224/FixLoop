"""Agent 运行时异常定义（V1.5-Bonus1c）。"""

from __future__ import annotations


class EmptyModelResponse(Exception):
    """模型返回空响应（无 HTTP body）。"""

    def __init__(self, model: str = "", detail: str = ""):
        self.model = model
        self.detail = detail or "模型返回空响应"
        super().__init__(self.detail)


class ContextTooLargeError(Exception):
    """Prompt 上下文超出 HARD_CAP 硬顶限制，拒绝执行。"""

    def __init__(self, actual: int, limit: int):
        self.actual = actual
        self.limit = limit
        detail = f"Prompt {actual} tokens 超出硬顶限制 ({limit})"
        super().__init__(detail)
