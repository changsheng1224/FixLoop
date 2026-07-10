"""System Prompt 构建：组装发送给模型的系统提示词。

包含：规则 → 工具列表（含签名和风险标记）→ 调用示例 → Workspace 快照。
稳定段（persona/rules/tools）参与 prompt cache hash；examples/skills 与 workspace 不参与 hash。
rules / examples 可外置至 `.agent/rules.md`、`.agent/examples.md`（见 prompt_external）。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from agent_runtime.prefix_stable import assert_stable_prefix_clean, hash_stable_prefix
from agent_runtime.prompt_external import (
    BUILTIN_TOOL_EXAMPLES,
    PromptAssets,
    compose_examples,
    compose_rules,
    load_prompt_assets,
)

__all__ = [
    "PromptPrefix",
    "TOOL_EXAMPLES",
    "build_custom_system_prefix",
    "build_prompt_prefix",
    "build_repair_agent_prefix",
    "join_stable_parts",
    "cache_stable_text",
]

# 向后兼容：few-shot 内置条目
TOOL_EXAMPLES = BUILTIN_TOOL_EXAMPLES


def join_stable_parts(*parts: str) -> str:
    """拼接非空 stable 段。"""
    return "\n\n".join(p.strip() for p in parts if p and p.strip())


def cache_stable_text(stable_system_text: str, stable_tools_text: str) -> str:
    """参与 prompt_cache_key 的稳定段（system + tools）。"""
    return join_stable_parts(stable_system_text, stable_tools_text)


@dataclass
class PromptPrefix:
    """系统提示词前缀。"""

    text: str
    stable_text: str
    workspace_text: str
    hash: str  # cache_stable_text 的 SHA256
    workspace_fingerprint: str
    tool_signature: str  # 工具 schema 的 SHA256
    stable_system_text: str = ""
    stable_tools_text: str = ""
    stable_skills_text: str = ""
    role_text: str = ""  # L2 角色 prompt（不进 hash，fill 时并入 skills）
    assets_fingerprint: str = ""  # 外置 rules+examples 内容 hash


def _resolve_repo_root(workspace, repo_root: str | Path | None) -> Path:
    if repo_root is not None:
        return Path(repo_root)
    for attr in ("repo_root", "cwd"):
        value = getattr(workspace, attr, "") or ""
        if value:
            return Path(value)
    return Path(".")


def _resolve_assets(workspace, repo_root: str | Path | None, assets: PromptAssets | None) -> PromptAssets:
    if assets is not None:
        return assets
    return load_prompt_assets(_resolve_repo_root(workspace, repo_root))


def _filter_tools(
    tools_registry: dict,
    tool_names: set[str] | tuple[str, ...] | None,
) -> dict:
    if tool_names is None:
        return tools_registry
    allowed = set(tool_names)
    return {k: v for k, v in tools_registry.items() if k in allowed}


def _make_prompt_prefix(
    stable_system_text: str,
    stable_tools_text: str,
    stable_skills_text: str,
    workspace,
    tool_signature: str,
    assets_fingerprint: str = "",
    *,
    role_text: str = "",
) -> PromptPrefix:
    for part in (stable_system_text, stable_tools_text, stable_skills_text, role_text):
        if part:
            assert_stable_prefix_clean(part)

    cache_text = cache_stable_text(stable_system_text, stable_tools_text)
    if not cache_text:
        cache_text = stable_system_text.strip()

    stable_text = join_stable_parts(stable_system_text, stable_tools_text, stable_skills_text)
    workspace_text = workspace.text()
    parts = [stable_text]
    if role_text:
        parts.append(role_text)
    if workspace_text:
        parts.append(workspace_text)
    text = "\n\n".join(parts)
    return PromptPrefix(
        text=text,
        stable_text=stable_text,
        stable_system_text=stable_system_text.strip(),
        stable_tools_text=stable_tools_text.strip(),
        stable_skills_text=stable_skills_text.strip(),
        workspace_text=workspace_text,
        hash=hash_stable_prefix(cache_text),
        workspace_fingerprint=workspace.fingerprint(),
        tool_signature=tool_signature,
        role_text=role_text,
        assets_fingerprint=assets_fingerprint,
    )


def build_prompt_prefix(
    workspace,
    tools_registry: dict,
    dry_run: bool = False,
    approval: str = "ask",
    *,
    tool_names: set[str] | tuple[str, ...] | None = None,
    repo_root: str | Path | None = None,
    assets: PromptAssets | None = None,
) -> PromptPrefix:
    """构建 System Prompt 前缀。

    L0：tool_names 非空时 prefix 仅注入启用工具签名。
    """
    tools_registry = _filter_tools(tools_registry, tool_names)
    prompt_assets = _resolve_assets(workspace, repo_root, assets)
    stable_system_text = join_stable_parts(
        _system_persona(),
        compose_rules(prompt_assets, dry_run=dry_run, approval=approval),
    )
    stable_tools_text = _tools_section(tools_registry)
    stable_skills_text = compose_examples(prompt_assets)
    tool_sig = _tool_signature(tools_registry)
    return _make_prompt_prefix(
        stable_system_text,
        stable_tools_text,
        stable_skills_text,
        workspace,
        tool_sig,
        prompt_assets.fingerprint,
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
    tools_registry = _filter_tools(tools_registry, tool_names)
    prompt_assets = _resolve_assets(workspace, repo_root, assets)
    stable_system_text = join_stable_parts(
        compose_rules(prompt_assets, dry_run=dry_run, approval=approval),
        _repair_tool_gateway_note(),
    )
    stable_tools_text = _tools_section(tools_registry)
    stable_skills_text = compose_examples(prompt_assets)
    role_text = l2_role_prompt.strip()
    tool_sig = _tool_signature(tools_registry)
    return _make_prompt_prefix(
        stable_system_text,
        stable_tools_text,
        stable_skills_text,
        workspace,
        tool_sig,
        prompt_assets.fingerprint,
        role_text=role_text,
    )


def build_custom_system_prefix(system_prompt: str, workspace) -> PromptPrefix:
    """L2 角色 system prompt + workspace；稳定段仅为 system_prompt。"""
    stable_system_text = system_prompt.strip()
    return _make_prompt_prefix(
        stable_system_text,
        "",
        "",
        workspace,
        tool_signature="",
    )


def _system_persona() -> str:
    return (
        "你是一个本地编码 Agent（agent_runtime）。"
        "你的任务是帮助用户理解、分析和修改代码仓库。"
        "你需要使用提供的工具来探索代码库并完成任务。"
        "注意：工具执行结果中如果出现 [DRY RUN] 前缀，表示当前在演习模式下运行，"
        "不会实际修改文件。你仍应基于 DRY RUN 结果规划后续步骤。"
    )


def _repair_tool_gateway_note() -> str:
    return (
        "**7. 工具可见性与权限**：下列为 repair 流水线工具全集；"
        "实际可调用范围由运行时权限控制。若调用被拒绝，请换用其他工具或返回 <final>。"
    )


def _tools_section(registry: dict) -> str:
    """从工具注册表生成工具列表描述（模型只见 schema，不含 run）。"""
    from agent_runtime.tool_schema import tool_schema_view

    view = tool_schema_view(registry)
    lines = ["## 可用工具", ""]
    for name in sorted(view):
        spec = view[name]
        risk = "⚠ 高风险" if spec.get("risky") else "✓ 安全"
        schema_str = json.dumps(spec.get("schema", {}), ensure_ascii=False)
        lines.append(f"### {name} [{risk}]")
        lines.append(f"参数: {schema_str}")
        desc = spec.get("description", "")
        if desc:
            lines.append(f"说明: {desc}")
        lines.append("")
    return "\n".join(lines)


def _tool_signature(registry: dict) -> str:
    """计算工具注册表的 SHA256 签名（用于 prompt cache 检测变更）。"""
    import hashlib

    schemas = {name: spec.get("schema", {}) for name, spec in sorted(registry.items())}
    return hashlib.sha256(
        json.dumps(schemas, ensure_ascii=False, sort_keys=True).encode()
    ).hexdigest()
