"""L0 Tier Guard：组装前过滤不该进 context 窗口的内容。

TierPolicy 在 ContextManager.build 与 compression_pipeline L0 阶段共用。
只影响投影 / prompt，不修改 canonical session.history。
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from typing import Literal

SkillMode = Literal["off", "index", "full"]

DEFAULT_PIN_ROLES = frozenset({"user"})
PIN_CONTENT_MARKERS = ("[Earlier summary]",)
PIN_ERROR_KEYWORDS = ("Traceback", "Fail", "FAILED", "error:")
LOW_VALUE_SYSTEM_PHRASES = ("工具调用格式错误",)

REJECTED_TOOL_MARKERS = (
    "不在允许列表中",
    "不可用。",
    "未注册。",
    "重复调用检测",
    "超出配额限制",
    "参数校验失败",
    "审批被拒绝",
)


@dataclass
class TierPolicy:
    """L0 分级策略：控制 section 注入与 history 投影过滤。"""

    allowed_tools: frozenset[str] = field(default_factory=frozenset)
    pin_roles: frozenset[str] = DEFAULT_PIN_ROLES
    drop_rejected_tools: bool = True
    drop_empty_content: bool = True
    drop_low_value_system: bool = True
    min_relevance_score: float = 0.0
    skill_mode: SkillMode = "off"
    current_turn_id: int | None = None
    protect_current_turn: bool = True
    tail_protect_tokens: int = 20_000

    @classmethod
    def from_agent(cls, agent, **overrides) -> TierPolicy:
        """从 Agent 实例构建默认策略。"""
        from agent_runtime.compression_pipeline import TAIL_PROTECT_TOKENS
        from agent_runtime.turn_tracking import current_turn_id

        tools = getattr(agent, "_tool_names", None)
        if tools is None:
            tools = set(getattr(agent, "tools", {}).keys())
        session = getattr(agent, "session", {}) or {}
        cfg = getattr(agent, "config", None)
        tail = getattr(cfg, "tail_protect_tokens", TAIL_PROTECT_TOKENS)
        base = cls(
            allowed_tools=frozenset(tools),
            current_turn_id=current_turn_id(session),
            tail_protect_tokens=tail,
        )
        if overrides:
            return replace(base, **overrides)
        return base

    def allows_tool(self, tool_name: str) -> bool:
        if not self.allowed_tools:
            return True
        if not tool_name:
            return True
        return tool_name in self.allowed_tools


def is_pinned_history_item(item: dict, policy: TierPolicy) -> bool:
    role = str(item.get("role", ""))
    if role in policy.pin_roles:
        return True
    content = str(item.get("content", ""))
    if any(marker in content for marker in PIN_CONTENT_MARKERS):
        return True
    if any(keyword in content for keyword in PIN_ERROR_KEYWORDS):
        return True
    if item.get("_snip") or item.get("_collapsed"):
        return True
    if (
        policy.protect_current_turn
        and policy.current_turn_id is not None
        and item.get("turn_id") == policy.current_turn_id
    ):
        return True
    return False


def is_rejected_tool_content(content: str) -> bool:
    text = content.strip()
    if not text.startswith("Error:"):
        return False
    return any(marker in text for marker in REJECTED_TOOL_MARKERS)


def is_low_value_system_content(content: str) -> bool:
    text = content.strip()
    return any(phrase in text for phrase in LOW_VALUE_SYSTEM_PHRASES)


def l0_filter_history(history: list[dict], policy: TierPolicy) -> tuple[list[dict], dict]:
    """L0 history 投影过滤。返回 (filtered_copy, stats)。"""
    kept: list[dict] = []
    stats = {
        "dropped": 0,
        "rules_applied": [],
    }
    rule_counts: dict[str, int] = {}

    def _drop(rule: str) -> None:
        stats["dropped"] += 1
        rule_counts[rule] = rule_counts.get(rule, 0) + 1

    for item in history:
        role = str(item.get("role", ""))
        content = str(item.get("content", ""))

        if role == "tool":
            tool_name = str(item.get("tool_name", ""))
            if not policy.allows_tool(tool_name):
                _drop("disallowed_tool")
                continue
            if policy.drop_rejected_tools and is_rejected_tool_content(content):
                _drop("rejected_tool")
                continue

        if is_pinned_history_item(item, policy):
            kept.append(dict(item))
            continue

        if policy.drop_empty_content and not content.strip():
            _drop("empty_content")
            continue

        if (
            policy.drop_low_value_system
            and role == "system"
            and is_low_value_system_content(content)
        ):
            _drop("low_value_system")
            continue

        kept.append(dict(item))

    stats["rules_applied"] = [f"{k}:{v}" for k, v in sorted(rule_counts.items())]
    return kept, stats


def filter_relevant_results(results: list[dict], policy: TierPolicy) -> list[dict]:
    """L0 relevant section：按最低 relevance score 过滤 episodic 检索结果。"""
    if policy.min_relevance_score <= 0:
        return results
    filtered: list[dict] = []
    for item in results:
        score = item.get("score")
        if score is None:
            filtered.append(item)
            continue
        if float(score) >= policy.min_relevance_score:
            filtered.append(item)
    return filtered
