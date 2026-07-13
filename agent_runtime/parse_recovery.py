"""解析失败 recovery prompt：片段 + caret + 修正指令。"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Literal

ParseFailureKind = Literal[
    "json_in_tool",
    "invalid_tool_payload",
    "wrong_xml_tag",
    "unclosed_tag",
    "empty",
    "unrecognized",
]

DEFAULT_SNIPPET_CHARS = 500


@dataclass(frozen=True)
class ParseFailure:
    kind: ParseFailureKind
    snippet: str
    error_offset: int | None
    error_message: str
    hint: str


@dataclass(frozen=True)
class ParseRetry:
    """retry payload：对外 str() 为 recovery prompt，附带结构化 failure。"""

    prompt: str
    failure: ParseFailure
    has_last_tool_anchor: bool = False

    def __str__(self) -> str:
        return self.prompt


def truncate_snippet(text: str, *, max_chars: int = DEFAULT_SNIPPET_CHARS) -> str:
    if len(text) <= max_chars:
        return text
    return text[:max_chars]


def format_caret_line(snippet: str, offset: int) -> str:
    safe = max(0, min(offset, len(snippet)))
    return f"{' ' * safe}^"


def build_recovery_prompt(
    failure: ParseFailure,
    *,
    last_tool_call: dict | None = None,
) -> str:
    """构建四段式 recovery prompt。

    Sections:
    ① 上次成功 tool（name + args 摘要）
    ② 刚才输出（截断 raw）
    ③ 错误位置（snippet + caret）
    ④ 正确格式（schema 样例）
    """
    lines: list[str] = []

    # ① 上一次成功的 tool 调用
    if last_tool_call and last_tool_call.get("name"):
        name = last_tool_call.get("name", "?")
        args = last_tool_call.get("args", {})
        args_summary = ", ".join(
            f"{k}={str(v)[:60]}" for k, v in list(args.items())[:3]
        )
        lines.append("## ① 上一次成功的工具调用")
        lines.append(f"```\n{name}({args_summary})\n```")
        lines.append("")
    elif last_tool_call is not None:
        lines.append("## ① 无成功工具调用可参考")
        lines.append("")

    # ② 刚才的输出（截断）
    if failure.snippet:
        lines.append("## ② 刚才的输出")
        lines.append("```")
        lines.append(truncate_snippet(failure.snippet, max_chars=600))
        lines.append("```")
        lines.append("")

    # ③ 错误位置
    lines.append("## ③ 错误")
    lines.append(f"类型：{failure.hint}")
    if failure.error_message:
        lines.append(f"详情：{failure.error_message}")
    if failure.snippet and failure.error_offset is not None:
        lines.append("```")
        lines.append(failure.snippet[:200])
        lines.append(format_caret_line(failure.snippet, failure.error_offset))
        lines.append("```")
    lines.append("")

    # ④ 正确格式
    lines.append("## ④ 正确格式")
    if failure.kind in ("json_in_tool", "invalid_tool_payload"):
        lines.append("请修正 JSON 后重新输出完整的工具调用：")
        lines.append('```\n<tool>{"name":"工具名","args":{...}}</tool>\n```')
    elif failure.kind == "wrong_xml_tag":
        lines.append("请改用 <tool> 包裹 JSON 的格式：")
        lines.append('```\n<tool>{"name":"工具名","args":{...}}</tool>\n```')
    elif failure.kind == "unclosed_tag":
        lines.append("请补全 </tool> 闭合标签：")
        lines.append('```\n<tool>{"name":"工具名","args":{...}}</tool>\n```')
    elif failure.kind == "empty":
        lines.append("输出 <tool> 调用工具，或 <final> 返回答案：")
        lines.append('```\n<tool>{"name":"工具名","args":{...}}</tool>\n```')
        lines.append("```\n<final>你的答案</final>\n```")
    else:
        lines.append("调用工具：")
        lines.append('```\n<tool>{"name":"工具名","args":{...}}</tool>\n```')
        lines.append("返回答案：")
        lines.append("```\n<final>你的答案</final>\n```")
    return "\n".join(lines)


def failure_from_json_in_tool(json_text: str, exc: json.JSONDecodeError) -> ParseFailure:
    snippet = truncate_snippet(json_text)
    offset = exc.pos
    if offset is not None and offset >= len(snippet):
        offset = None
    message = exc.msg
    if exc.pos is not None:
        message = f"{exc.msg}（column {exc.pos + 1}）"
    return ParseFailure(
        kind="json_in_tool",
        snippet=snippet,
        error_offset=offset,
        error_message=message,
        hint="<tool> 内必须是合法 JSON",
    )


def failure_invalid_tool_payload(payload: object) -> ParseFailure:
    snippet = truncate_snippet(str(payload))
    return ParseFailure(
        kind="invalid_tool_payload",
        snippet=snippet,
        error_offset=None,
        error_message="tool payload 缺少 name 字段或类型无效",
        hint="<tool> JSON 必须包含 name 与 args",
    )


def _tool_inner_json(text: str) -> str:
    from agent_runtime.text_tags import extract_between_tags

    return extract_between_tags(text, "<tool>", "</tool>")


def diagnose_parse_failure(raw: str) -> ParseFailure:
    """根据原始输出诊断解析失败（与 Agent.parse 失败路径对齐）。"""
    text = raw.strip()
    if not text:
        return ParseFailure(
            kind="empty",
            snippet="",
            error_offset=None,
            error_message="模型返回空输出",
            hint="请输出 <tool> 或 <final>",
        )

    if "<tool>" in text and "</tool>" not in text:
        snippet = truncate_snippet(text)
        return ParseFailure(
            kind="unclosed_tag",
            snippet=snippet,
            error_offset=text.find("<tool>"),
            error_message="缺少闭合标签 </tool>",
            hint="<tool> 标签未闭合",
        )

    json_match = _tool_inner_json(text)
    if json_match:
        try:
            json.loads(json_match)
        except json.JSONDecodeError as exc:
            return failure_from_json_in_tool(json_match, exc)

    wrong = re.match(r"<(\w+)>", text)
    if wrong and wrong.group(1) not in ("tool", "final"):
        tag = wrong.group(1)
        snippet = truncate_snippet(text)
        return ParseFailure(
            kind="wrong_xml_tag",
            snippet=snippet,
            error_offset=0,
            error_message=f"不支持的工具标签 <{tag}>...</{tag}>",
            hint=f'请改用 <tool>{{"name":"{tag}","args":{{...}}}}</tool>',
        )

    return ParseFailure(
        kind="unrecognized",
        snippet=truncate_snippet(text),
        error_offset=None,
        error_message="输出不符合 <tool> 或 <final> 格式",
        hint="请严格使用 <tool> JSON 或 <final> 格式",
    )


def make_parse_retry(
    raw: str,
    failure: ParseFailure | None = None,
    *,
    last_tool_call: dict | None = None,
) -> ParseRetry:
    resolved = failure or diagnose_parse_failure(raw)
    prompt = build_recovery_prompt(resolved, last_tool_call=last_tool_call)
    return ParseRetry(prompt, resolved, has_last_tool_anchor=last_tool_call is not None)
