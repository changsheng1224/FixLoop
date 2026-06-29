"""System Prompt 构建：组装发送给模型的系统提示词。

包含：规则 → 工具列表（含签名和风险标记）→ 调用示例 → Workspace 快照。
"""

import hashlib
import json
from dataclasses import dataclass, field
from datetime import UTC, datetime


@dataclass
class PromptPrefix:
    """系统提示词前缀。"""

    text: str
    hash: str  # prefix_text 的 SHA256（用于 prompt cache key）
    workspace_fingerprint: str
    tool_signature: str  # 工具 schema 的 SHA256
    built_at: str = field(default_factory=lambda: datetime.now(UTC).isoformat())


# ============================================================================
# 示例：模型调用工具的正确格式
# ============================================================================

TOOL_EXAMPLES = [
    {
        "description": "列出当前目录的文件",
        "tool": '<tool>{"name":"list_files","args":{"path":"."}}</tool>',
    },
    {
        "description": "读取文件的前 50 行",
        "tool": (
            '<tool>{"name":"read_file",'
            '"args":{"path":"src/main.py","start":1,"end":50}}</tool>'
        ),
    },
    {
        "description": "搜索特定模式",
        "tool": '<tool>{"name":"search","args":{"pattern":"TODO","path":"src"}}</tool>',
    },
    {
        "description": "返回最终答案",
        "response": (
            "<final>问题已解决：原因是类型转换缺失，已在第 42 行添加了 int() 转换。</final>"
        ),
    },
]


def build_prompt_prefix(workspace, tools_registry: dict) -> PromptPrefix:
    """构建 System Prompt 前缀。

    Args:
        workspace: WorkspaceContext 实例，提供工作区快照。
        tools_registry: build_tool_registry() 返回的工具注册表。

    Returns:
        PromptPrefix 实例，包含 text、hash、fingerprint 和 tool_signature。
    """
    sections = [
        _system_persona(),
        _rules(),
        _tools_section(tools_registry),
        _examples_section(),
        workspace.text(),
    ]
    text = "\n\n".join(sections)

    return PromptPrefix(
        text=text,
        hash=hashlib.sha256(text.encode()).hexdigest(),
        workspace_fingerprint=workspace.fingerprint(),
        tool_signature=_tool_signature(tools_registry),
    )


def _system_persona() -> str:
    return (
        "你是一个本地编码 Agent（agent_runtime）。"
        "你的任务是帮助用户理解、分析和修改代码仓库。"
        "你需要使用提供的工具来探索代码库并完成任务。"
        "注意：工具执行结果中如果出现 [DRY RUN] 前缀，表示当前在演习模式下运行，"
        "不会实际修改文件。你仍应基于 DRY RUN 结果规划后续步骤。"
    )


def _rules() -> str:
    return (
        "## 规则\n\n"
        "1. 每次只调用一个工具，等待结果后再决定下一步。\n"
        "2. 通过工具探索代码库——不要猜测文件内容。\n"
        "3. 调用 read_file 时指定合理的行号范围（默认 1-200）。\n"
        "4. 工具调用使用 JSON 格式：<tool>{\"name\":\"tool_name\",\"args\":{...}}</tool>\n"
        "5. 最终答案使用 XML 格式：<final>你的答案</final>\n"
        "6. 答案必须基于实际读取的文件内容，不要捏造。\n"
        "7. 如果找不到答案，诚实告知而不是编造。"
    )


def _tools_section(registry: dict) -> str:
    """从工具注册表生成工具列表描述。"""
    lines = ["## 可用工具", ""]
    for name in sorted(registry.keys()):
        spec = registry[name]
        risk = "⚠ 高风险" if spec.get("risky") else "✓ 安全"
        schema_str = json.dumps(spec.get("schema", {}), ensure_ascii=False)
        lines.append(f"### {name} [{risk}]")
        lines.append(f"参数: {schema_str}")
        desc = spec.get("description", "")
        if desc:
            lines.append(f"说明: {desc}")
        lines.append("")
    return "\n".join(lines)


def _examples_section() -> str:
    """生成工具调用示例。"""
    lines = ["## 调用示例", ""]
    for i, ex in enumerate(TOOL_EXAMPLES, 1):
        lines.append(f"### 示例 {i}: {ex['description']}")
        if "tool" in ex:
            lines.append("**格式**: 工具调用（JSON 格式）")
            lines.append(f"```\n{ex['tool']}\n```")
        if "response" in ex:
            lines.append("**格式**: 最终答案")
            lines.append(f"```\n{ex['response']}\n```")
        lines.append("")
    return "\n".join(lines)


def _tool_signature(registry: dict) -> str:
    """计算工具注册表的 SHA256 签名（用于 prompt cache 检测变更）。"""
    schemas = {name: spec.get("schema", {}) for name, spec in sorted(registry.items())}
    return hashlib.sha256(
        json.dumps(schemas, ensure_ascii=False, sort_keys=True).encode()
    ).hexdigest()
