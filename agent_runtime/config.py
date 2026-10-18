"""Agent 配置系统：Pydantic 模型 + .env 加载 + CLI args 覆盖。

基于 pydantic.BaseModel，启动时校验所有配置项。
"""

from typing import Literal

from pydantic import BaseModel, Field

from agent_runtime.policy import RuntimePolicy


class WebConfig(BaseModel):
    """Host-authorized external research; credentials never enter config snapshots."""

    model_config = {"extra": "forbid", "validate_assignment": True}

    enabled: bool = False
    timeout_s: float = Field(default=20, gt=0, le=60)
    max_attempts: int = Field(default=2, ge=1, le=3)
    preview_chars: int = Field(default=12000, ge=1000, le=32000)
    summary_chars: int = Field(default=700, ge=100, le=2000)


class CodeExplorationConfig(BaseModel):
    mode: Literal["text", "lsp", "relations"] = "text"
    server_argv: tuple[str, ...] | None = None


class AgentConfig(RuntimePolicy):
    """Agent 运行时配置（Single Source of Truth）。

    配置来源优先级：CLI args > .env > 默认值。
    """

    web: WebConfig = Field(default_factory=WebConfig)
    code_exploration: CodeExplorationConfig = Field(default_factory=CodeExplorationConfig)
    auto_context_extraction: bool = Field(
        default=True, description="Plan repair 路径自动提取可信来源中的明确约束和节点决策"
    )

    tail_protect_tokens: int = Field(
        default=20_000,
        ge=0,
        le=100_000,
        description="history 尾部保护区 token 数（L2–L4 豁免 assistant/tool 所在 turn）",
    )
    final_schema: dict[str, str] | None = Field(
        default=None,
        description=(
            "final answer 结构校验：{字段名: 类型}，如 {'file_path':'str','line':'int'}。"
            "类型: str/int/float/bool/list/dict。仅 json_mode=True 时生效。"
            "校验失败 → recovery prompt + 回到 Acting（最多 2 次重试）。"
        ),
    )
