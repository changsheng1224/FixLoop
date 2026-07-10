"""Agent 配置系统：Pydantic 模型 + .env 加载 + CLI args 覆盖。

基于 pydantic.BaseModel，启动时校验所有配置项。
"""

from pydantic import BaseModel, Field


class AgentConfig(BaseModel):
    """Agent 运行时配置（Single Source of Truth）。

    配置来源优先级：CLI args > .env > 默认值。
    """

    provider: str = Field(
        default="deepseek", description="模型 Provider: deepseek / openai / ollama"
    )
    model: str = Field(default="deepseek-v4-pro", description="模型名称")
    max_steps: int = Field(default=6, ge=1, le=50, description="最大工具调用步数")
    tool_timeout_s: int = Field(
        default=120,
        ge=0,
        le=3600,
        description="单工具 Gate 9 执行超时秒数，0=禁用",
    )
    max_new_tokens: int = Field(
        default=2048, ge=1, le=8192, description="每次 LLM 调用的最大输出 token 数"
    )
    prompt_budget: int = Field(
        default=100_000,
        ge=512,
        le=100_000,
        description="单次 prompt 总 token 预算（system + user / 五 section 合计）",
    )
    tail_protect_tokens: int = Field(
        default=20_000,
        ge=0,
        le=100_000,
        description="history 尾部保护区 token 数（L2–L4 豁免 assistant/tool 所在 turn）",
    )
    approval: str = Field(default="ask", description="高风险工具审批策略: auto / ask / never")
    temperature: float = Field(default=0.2, ge=0.0, le=2.0, description="模型温度")
