"""Agent 运行时异常定义（V1.5-Bonus1c）。"""

from __future__ import annotations


class EmptyModelResponse(Exception):  # noqa: N818 - public API name kept stable
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

    @property
    def user_message(self) -> str:
        """返回用户可读的错误消息（含恢复建议）。"""
        return (
            f"<final>Prompt 大小 {self.actual} tokens 超出硬顶限制 ({self.limit})。"
            "请缩短输入或使用 /reset 清空对话历史后重试。</final>"
        )
