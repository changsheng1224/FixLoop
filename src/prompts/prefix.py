from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from agent_runtime.prefix_stable import assert_stable_prefix_clean
from agent_runtime.prompt_external import PromptAssets
from agent_runtime.prompt_prefix import PromptPrefix, build_prompt_prefix, join_stable_parts


@dataclass(frozen=True)
class RepairL1Prefix:
    """Repair 流水线共享 L1 稳定段（不含 L2 role）。"""

    stable_system_text: str
    stable_tools_text: str
    stable_skills_text: str
    workspace_text: str
    hash: str
    tool_signature: str
    assets_fingerprint: str
    workspace_fingerprint: str


def build_repair_l1_prefix(
    workspace,
    tools_registry: dict,
    dry_run: bool = False,
    approval: str = "ask",
    *,
    repo_root: str | Path | None = None,
    assets: PromptAssets | None = None,
) -> RepairL1Prefix:
    """构建 repair 全流程共享的 L1 稳定段（Orchestrator 构建一次）。"""
    prefix = build_prompt_prefix(
        workspace,
        tools_registry,
        dry_run=dry_run,
        approval=approval,
        repo_root=repo_root,
        assets=assets,
        persona="",
        additional_rules=_repair_tool_gateway_note(),
    )
    bundle = prefix

    return RepairL1Prefix(
        stable_system_text=bundle.stable_system_text,
        stable_tools_text=bundle.stable_tools_text,
        stable_skills_text=bundle.stable_skills_text,
        workspace_text=workspace.text(),
        hash=bundle.hash,
        tool_signature=bundle.tool_signature,
        assets_fingerprint=bundle.assets_fingerprint,
        workspace_fingerprint=workspace.fingerprint(),
    )


def compose_repair_prefix(l1: RepairL1Prefix, role_text: str) -> PromptPrefix:
    """L1 共享束 + L2 角色 prompt → 完整 PromptPrefix。"""
    role_text = role_text.strip()
    if role_text:
        assert_stable_prefix_clean(role_text)
    stable_text = join_stable_parts(
        l1.stable_system_text, l1.stable_tools_text, l1.stable_skills_text
    )
    parts = [stable_text]
    if role_text:
        parts.append(role_text)
    if l1.workspace_text:
        parts.append(l1.workspace_text)
    return PromptPrefix(
        text="\n\n".join(parts),
        stable_text=stable_text,
        stable_system_text=l1.stable_system_text,
        stable_tools_text=l1.stable_tools_text,
        stable_skills_text=l1.stable_skills_text,
        workspace_text=l1.workspace_text,
        hash=l1.hash,
        workspace_fingerprint=l1.workspace_fingerprint,
        tool_signature=l1.tool_signature,
        role_text=role_text,
        assets_fingerprint=l1.assets_fingerprint,
    )


def build_repair_agent_prefix(
    l2_role_prompt: str,
    workspace,
    tools_registry: dict,
    dry_run: bool = False,
    approval: str = "ask",
    *,
    tool_names: tuple[str, ...] | set[str] | None = None,
    repo_root: str | Path | None = None,
    assets: PromptAssets | None = None,
) -> PromptPrefix:
    """Repair 双层 prefix：L1 stable（rules+tools+examples）+ L2 role（不进 hash）。"""
    tools_registry = {
        k: v for k, v in tools_registry.items() if tool_names is None or k in tool_names
    }
    l1 = build_repair_l1_prefix(
        workspace,
        tools_registry,
        dry_run=dry_run,
        approval=approval,
        repo_root=repo_root,
        assets=assets,
    )
    return compose_repair_prefix(l1, l2_role_prompt)


def _repair_tool_gateway_note() -> str:
    return (
        "**7. 工具可见性与权限**：下列为 repair 流水线工具全集；"
        "实际可调用范围由运行时权限控制。若调用被拒绝，请换用其他工具或返回 <final>。"
    )
