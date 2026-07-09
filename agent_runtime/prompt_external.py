"""外置 Prompt 资产：`.agent/rules.md` 与 `.agent/examples.md` 加载与组装。"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path

from agent_runtime.prefix_stable import assert_stable_prefix_clean

__all__ = [
    "BUILTIN_TOOL_EXAMPLES",
    "PromptAssets",
    "compose_examples",
    "compose_rules",
    "default_examples_text",
    "default_rules_text",
    "load_prompt_assets",
]

RULES_FILENAME = "rules.md"
EXAMPLES_FILENAME = "examples.md"

BUILTIN_TOOL_EXAMPLES = [
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


@dataclass(frozen=True)
class PromptAssets:
    """rules / examples 源文本与 fingerprint（参与 cache invalidation）。"""

    rules_text: str
    examples_text: str
    fingerprint: str
    rules_source: str
    examples_source: str


def default_rules_text() -> str:
    """内置 rules 主体（不含 dry_run / approval 运行时后缀）。"""
    return "\n".join(
        [
            "## 核心规则（必须严格遵守）",
            "",
            "**1. 工具调用格式**（任选其一）：",
            '   格式A (JSON): <tool>{"name":"工具名","args":{"参数名":"值"}}</tool>',
            "   格式B (function_calls):",
            "     <function_calls>",
            '     <invoke name="工具名">',
            '     <parameter name="参数名">值</parameter>',
            "     </invoke>",
            "     </function_calls>",
            "   推荐使用格式B（function_calls）。",
            "",
            "**2. 最终答案格式**：",
            "   <final>你的答案</final>",
            "",
            "**3. 每次只调用一个工具**，等待结果后再决定下一步。",
            "**4. 通过工具探索代码库**——不要猜测文件内容。",
            "**5. 答案必须基于实际读取的文件内容**，不要捏造。",
            "**6. 如果找不到答案，诚实告知而不是编造。**",
        ]
    )


def render_examples(entries: list[dict]) -> str:
    """将 few-shot 条目渲染为 ## 调用示例 段。"""
    lines = ["## 调用示例", ""]
    for i, ex in enumerate(entries, 1):
        lines.append(f"### 示例 {i}: {ex['description']}")
        if "tool" in ex:
            lines.append("**格式**: 工具调用（JSON 格式）")
            lines.append(f"```\n{ex['tool']}\n```")
        if "response" in ex:
            lines.append("**格式**: 最终答案")
            lines.append(f"```\n{ex['response']}\n```")
        lines.append("")
    return "\n".join(lines).rstrip()


def default_examples_text() -> str:
    return render_examples(BUILTIN_TOOL_EXAMPLES)


def _assets_fingerprint(rules_text: str, examples_text: str) -> str:
    payload = f"{rules_text}\n---\n{examples_text}"
    return hashlib.sha256(payload.encode()).hexdigest()


def load_prompt_assets(repo_root: str | Path) -> PromptAssets:
    """从 repo `.agent/` 加载 rules/examples；缺失则使用内置 default。"""
    root = Path(repo_root)
    agent_dir = root / ".agent"
    rules_path = agent_dir / RULES_FILENAME
    examples_path = agent_dir / EXAMPLES_FILENAME

    if rules_path.is_file():
        rules_text = rules_path.read_text(encoding="utf-8").strip()
        assert_stable_prefix_clean(rules_text)
        rules_source = "repo:.agent/rules.md"
    else:
        rules_text = default_rules_text()
        rules_source = "builtin"

    if examples_path.is_file():
        examples_text = examples_path.read_text(encoding="utf-8").strip()
        assert_stable_prefix_clean(examples_text)
        examples_source = "repo:.agent/examples.md"
    else:
        examples_text = default_examples_text()
        examples_source = "builtin"

    return PromptAssets(
        rules_text=rules_text,
        examples_text=examples_text,
        fingerprint=_assets_fingerprint(rules_text, examples_text),
        rules_source=rules_source,
        examples_source=examples_source,
    )


def compose_rules(
    assets: PromptAssets,
    *,
    dry_run: bool = False,
    approval: str = "ask",
) -> str:
    """rules 主体 + 运行时 dry_run / approval 后缀。"""
    rules = assets.rules_text.rstrip()
    extras: list[str] = []
    if dry_run:
        extras.append(
            "8. 当前是演习模式（Dry-Run），工具返回 [DRY RUN] 表示不会实际执行。"
            "你仍应基于 DRY RUN 结果输出完整方案。"
        )
    if approval == "auto":
        extras.append(
            "9. 你拥有自动审批权限，可以直接修改文件和执行命令。谨慎使用这些权限，只做必要的修改。"
        )
    if extras:
        return rules + "\n" + "\n".join(extras)
    return rules


def compose_examples(assets: PromptAssets) -> str:
    """examples 段；repo 文件缺标题时自动补 ## 调用示例。"""
    text = assets.examples_text.strip()
    if not text.startswith("## 调用示例"):
        text = f"## 调用示例\n\n{text}"
    return text
