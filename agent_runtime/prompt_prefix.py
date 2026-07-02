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
        "description": "读取文件（推荐格式：function_calls）",
        "tool": (
            "<function_calls>\n"
            '<invoke name="read_file">\n'
            '<parameter name="path">src/main.py</parameter>\n'
            '<parameter name="start">1</parameter>\n'
            '<parameter name="end">50</parameter>\n'
            "</invoke>\n"
            "</function_calls>"
        ),
    },
    {
        "description": "列出当前目录的文件（JSON格式也可）",
        "tool": '<tool>{"name":"list_files","args":{"path":"."}}</tool>',
    },
    {
        "description": "搜索特定模式",
        "tool": (
            "<function_calls>\n"
            '<invoke name="search">\n'
            '<parameter name="pattern">TODO</parameter>\n'
            '<parameter name="path">src</parameter>\n'
            "</invoke>\n"
            "</function_calls>"
        ),
    },
    {
        "description": "返回最终答案",
        "response": (
            "<final>问题已解决：原因是类型转换缺失，已在第 42 行添加了 int() 转换。</final>"
        ),
    },
]


def build_prompt_prefix(
    workspace, tools_registry: dict,
    dry_run: bool = False, approval: str = "ask",
) -> PromptPrefix:
    """构建 System Prompt 前缀。"""
    sections = [
        _system_persona(),
        _rules(dry_run=dry_run, approval=approval),
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


def _rules(dry_run: bool = False, approval: str = "ask") -> str:
    rules = [
        "## 核心规则（必须严格遵守）",
        "",
        "**1. 工具调用格式**（任选其一）：",
        '   格式A (JSON): <tool>{"name":"工具名","args":{"参数名":"值"}}</tool>',
        '   格式B (function_calls):',
        '     <function_calls>',
        '     <invoke name="工具名">',
        '     <parameter name="参数名">值</parameter>',
        '     </invoke>',
        '     </function_calls>',
        "   推荐使用格式B（function_calls）。",
        "",
        "**2. 最终答案格式**：",
        "   <final>你的答案</final>",
        "",
        "**3. 每次只调用一个工具**，等待结果后再决定下一步。",
        "**4. 通过工具探索代码库**——不要猜测文件内容。",
        "**5. 答案必须基于实际读取的文件内容**，不要捏造。",
        "**6. 如果找不到答案，诚实告知而不是编造。",
    ]
    if dry_run:
        rules.append(
            "8. 当前是演习模式（Dry-Run），工具返回 [DRY RUN] 表示不会实际执行。"
            "你仍应基于 DRY RUN 结果输出完整方案。"
        )
    if approval == "auto":
        rules.append(
            "9. 你拥有自动审批权限，可以直接修改文件和执行命令。"
            "谨慎使用这些权限，只做必要的修改。"
        )
    return "\n".join(rules)


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
