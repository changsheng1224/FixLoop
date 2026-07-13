"""Agent 预热上下文：分词器预热 + Agent 并行构建。

WarmContext 预计算 Agent 构造与首次 ask() 中的昂贵部分，降低 repair 启动与首轮延迟。

预热范围：
1. 分词器首次加载（模块级缓存，跨 Agent 共享）—— 首轮 ask() 的 ContextManager 冷启动
2. ~后续可扩展：memory 投影预计算、system prompt 预加载~

线程安全：WarmContext 本身不可变；warm_tokenizer 内部命中模块级缓存，多线程安全。

Usage::

    wc = WarmContext(model="deepseek-v4-pro", provider="deepseek")
    wc.warm_tokenizer()          # 强制加载分词器到模块级缓存
    agent = Agent(...)            # 首次 ask() 不再需要加载分词器
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class WarmContext:
    """预计算 Agent 运行时昂贵部分。

    Attributes:
        model: 模型名（用于选择 tokenizer）。
        provider: 提供商名。
        _tokenizer_warmed: 是否已预热分词器。
    """

    model: str = "deepseek-v4-pro"
    provider: str = "deepseek"
    _tokenizer_warmed: bool = field(default=False, repr=False)

    def warm_tokenizer(self) -> None:
        """强制加载分词器到模块级缓存。

        首次调用触发 HuggingFace ``tokenizers`` 或 ``tiktoken`` 加载（~0.5–1.5s）；
        后续调用命中 :func:`~agent_runtime.tokenizers.resolve_token_counter` 的
        模块级 ``_COUNTER_CACHE``，O(1) 返回。

        幂等：重复调用仅命中缓存，无额外开销。
        """
        if self._tokenizer_warmed:
            return
        from agent_runtime.tokenizers import resolve_token_counter

        resolve_token_counter(self.model, self.provider)
        self._tokenizer_warmed = True

    def warm_all(self) -> None:
        """执行全部预热步骤（当前为 tokenizer + 预留扩展点）。"""
        self.warm_tokenizer()


def create_warm_context(
    model: str = "deepseek-v4-pro",
    provider: str = "deepseek",
) -> WarmContext:
    """工厂函数：创建并预热 WarmContext。

    Returns:
        已完成 tokenizer 预热的 WarmContext 实例。
    """
    wc = WarmContext(model=model, provider=provider)
    wc.warm_all()
    return wc
