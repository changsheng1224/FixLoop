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
    PromptAssets,
    compose_examples,
    compose_rules,
    load_prompt_assets,
)

__all__ = [
    "PromptPrefix",
    "build_custom_system_prefix",
    "build_prefix_hashes",
    "build_prompt_prefix",
    "join_stable_parts",
    "cache_stable_text",
]


def join_stable_parts(*parts: str) -> str:
    """拼接非空 stable 段。"""
    return "\n\n".join(p.strip() for p in parts if p and p.strip())


def cache_stable_text(stable_system_text: str, stable_tools_text: str) -> str:
    """参与 prompt_cache_key 的稳定段（system + tools）。"""
    return join_stable_parts(stable_system_text, stable_tools_text)


def build_prefix_hashes(prefix: PromptPrefix) -> dict[str, str]:
    """分段 hash 观测字段；cache_key 与 prompt_cache_key 一致。"""
    system = prefix.stable_system_text or ""
    tools = prefix.stable_tools_text or ""
    skills = prefix.stable_skills_text or ""
    role = prefix.role_text or ""
    return {
        "system": hash_stable_prefix(system) if system else "",
        "tools": hash_stable_prefix(tools) if tools else "",
        "skills": hash_stable_prefix(skills) if skills else "",
        "cache_key": prefix.hash or "",
        "role": hash_stable_prefix(role) if role else "",
        "tool_signature": prefix.tool_signature or "",
        "assets_fingerprint": prefix.assets_fingerprint or "",
        "workspace_fingerprint": prefix.workspace_fingerprint or "",
    }


@dataclass
class PromptPrefix:
    """系统提示词前缀。"""

    text: str
    stable_text: str
    workspace_text: str
    hash: str
    workspace_fingerprint: str
    tool_signature: str
    stable_system_text: str = ""
    stable_tools_text: str = ""
    stable_skills_text: str = ""
    role_text: str = ""
    assets_fingerprint: str = ""


def _resolve_repo_root(workspace, repo_root: str | Path | None) -> Path:
    if repo_root is not None:
        return Path(repo_root)
    for attr in ("repo_root", "cwd"):
        value = getattr(workspace, attr, "") or ""
        if value:
            return Path(value)
    return Path(".")


def _resolve_assets(
    workspace, repo_root: str | Path | None, assets: PromptAssets | None
) -> PromptAssets:
    if assets is not None:
        return assets
    return load_prompt_assets(_resolve_repo_root(workspace, repo_root))


def _filter_tools(tools_registry: dict, tool_names: set[str] | tuple[str, ...] | None) -> dict:
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


@dataclass(frozen=True)
class _StableSectionBundle:
    stable_system_text: str
    stable_tools_text: str
    stable_skills_text: str
    tool_signature: str
    assets_fingerprint: str


def _assemble_stable_sections(
    prompt_assets: PromptAssets,
    tools_registry: dict,
    *,
    persona: str,
    additional_rules: str,
    dry_run: bool,
    approval: str,
) -> _StableSectionBundle:
    stable_system_text = join_stable_parts(
        persona, compose_rules(prompt_assets, dry_run=dry_run, approval=approval), additional_rules
    )
    stable_tools_text = _tools_section(tools_registry)
    stable_skills_text = compose_examples(prompt_assets)
    for part in (stable_system_text, stable_tools_text, stable_skills_text):
        if part:
            assert_stable_prefix_clean(part)
    return _StableSectionBundle(
        stable_system_text=stable_system_text.strip(),
        stable_tools_text=stable_tools_text.strip(),
        stable_skills_text=stable_skills_text.strip(),
        tool_signature=_tool_signature(tools_registry),
        assets_fingerprint=prompt_assets.fingerprint,
    )


def _cache_hash_from_bundle(bundle: _StableSectionBundle) -> str:
    cache_text = cache_stable_text(bundle.stable_system_text, bundle.stable_tools_text)
    if not cache_text:
        cache_text = bundle.stable_system_text
    return hash_stable_prefix(cache_text)


def build_prompt_prefix(
    workspace,
    tools_registry: dict,
    dry_run: bool = False,
    approval: str = "ask",
    *,
    tool_names: set[str] | tuple[str, ...] | None = None,
    repo_root: str | Path | None = None,
    assets: PromptAssets | None = None,
    persona: str | None = None,
    additional_rules: str = "",
) -> PromptPrefix:
    """构建 System Prompt 前缀。

    L0：tool_names 非空时 prefix 仅注入启用工具签名。
    """
    tools_registry = _filter_tools(tools_registry, tool_names)
    prompt_assets = _resolve_assets(workspace, repo_root, assets)
    bundle = _assemble_stable_sections(
        prompt_assets,
        tools_registry,
        persona=_system_persona() if persona is None else persona,
        additional_rules=additional_rules,
        dry_run=dry_run,
        approval=approval,
    )
    return _make_prompt_prefix(
        bundle.stable_system_text,
        bundle.stable_tools_text,
        bundle.stable_skills_text,
        workspace,
        bundle.tool_signature,
        bundle.assets_fingerprint,
    )


def build_custom_system_prefix(system_prompt: str, workspace) -> PromptPrefix:
    """L2 角色 system prompt + workspace；稳定段仅为 system_prompt。"""
    stable_system_text = system_prompt.strip()
    return _make_prompt_prefix(stable_system_text, "", "", workspace, tool_signature="")


def _system_persona() -> str:
    return (
        "你是一个本地编码 Agent（agent_runtime）。你的任务是帮助用户理解、分析和修改代码仓库。"
        "你需要使用提供的工具来探索代码库并完成任务。注意：工具执行结果中如果出现 [DRY RUN] "
        "前缀，表示当前在演习模式下运行，不会实际修改文件。"
        "你仍应基于 DRY RUN 结果规划后续步骤。"
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
