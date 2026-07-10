"""解析失败 recovery prompt：片段 + caret + 修正指令。"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Literal

ParseFailureKind = Literal[
    "json_in_tool",
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

    def __str__(self) -> str:
        return self.prompt


def truncate_snippet(text: str, *, max_chars: int = DEFAULT_SNIPPET_CHARS) -> str:
    if len(text) <= max_chars:
        return text
    return text[:max_chars]


def format_caret_line(snippet: str, offset: int) -> str:
    safe = max(0, min(offset, len(snippet)))
    return f"{' ' * safe}^"


def build_recovery_prompt(failure: ParseFailure) -> str:
    lines = [f"解析失败：{failure.hint}", ""]
    if failure.snippet:
        lines.extend(["片段：", failure.snippet])
        if failure.error_offset is not None:
            lines.append(format_caret_line(failure.snippet, failure.error_offset))
    if failure.error_message:
        lines.append(f"错误：{failure.error_message}")
    lines.append("")
    if failure.kind == "json_in_tool":
        lines.append("请仅修正上述 JSON 后重新输出完整 <tool>...</tool>。")
        lines.append('正确示例：<tool>{"name":"工具名","args":{...}}</tool>')
    elif failure.kind == "wrong_xml_tag":
        lines.append("请改用 <tool> 包裹 JSON 的格式重新调用。")
    elif failure.kind == "unclosed_tag":
        lines.append("请补全 </tool> 并确保 <tool> 内为合法 JSON。")
    elif failure.kind == "empty":
        lines.append("请输出 <tool>{...}</tool> 调用工具，或 <final>...</final> 返回答案。")
    else:
        lines.append("请修正后重新输出。")
        lines.append('  调用工具: <tool>{"name":"工具名","args":{...}}</tool>')
        lines.append("  返回答案: <final>你的答案</final>")
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


def _extract_json_between_tags(text: str, open_tag: str, close_tag: str) -> str:
    start = text.find(open_tag)
    if start == -1:
        return ""
    start += len(open_tag)
    end = text.find(close_tag, start)
    if end == -1:
        return ""
    return text[start:end].strip()


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

    json_match = _extract_json_between_tags(text, "<tool>", "</tool>")
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


def make_parse_retry(raw: str, failure: ParseFailure | None = None) -> ParseRetry:
    resolved = failure or diagnose_parse_failure(raw)
    return ParseRetry(build_recovery_prompt(resolved), resolved)
