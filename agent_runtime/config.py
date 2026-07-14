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
    step_timeout_s: int = Field(
        default=300,
        ge=0,
        le=7200,
        description="单步 wall-clock 超时秒数（context+model+tool），0=禁用",
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
    json_mode: bool = Field(default=False, description="启用 JSON 输出引导（repair agent 专用）")
    hard_cap: int = Field(
        default=8000,
        ge=512,
        le=200_000,
        description="Prompt 上下文硬顶 token 数。超出时拒绝 ask，不静默裁剪。",
    )
    max_llm_calls_per_repair: int = Field(
        default=0,
        ge=0,
        le=200,
        description="单次 repair LLM 调用硬顶（0=不禁用）。超限触发 budget_exhausted。",
    )
    loop_detect_threshold: int = Field(
        default=3,
        ge=0,
        le=10,
        description="死循环检测阈值：滑动窗口内同一 (tool, args_hash) 次数≥K 时触发。0=禁用",
    )
    final_schema: dict[str, str] | None = Field(
        default=None,
        description=(
            "final answer 结构校验：{字段名: 类型}，如 {'file_path':'str','line':'int'}。"
            "类型: str/int/float/bool/list/dict。仅 json_mode=True 时生效。"
            "校验失败 → recovery prompt + 回到 Acting（最多 2 次重试）。"
        ),
    )
