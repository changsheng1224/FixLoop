"""Retriever 终态工具：以 tool args 提交 RetrievedContext（Structured via Tool Calling）。"""

from __future__ import annotations

import json
from dataclasses import dataclass

from agent_runtime.schema_utils import auto_schema
from src.state import RetrievedContext

SUBMIT_RETRIEVED_CONTEXT = "submit_retrieved_context"


@dataclass
class SubmitRetrievedContextArgs:
    """终态提交参数（数组字段经 tool schema 暴露为 JSON array）。"""

    related_tests: list[str]
    caller_locations: list[str] | None = None
    similar_snippets: list[str] | None = None
    similar_fixes: list[str] | None = None


def _as_str_list(value) -> list[str]:
    if value is None:
        return []
    if isinstance(value, list):
        out: list[str] = []
        for item in value:
            if isinstance(item, str):
                if item.strip():
                    out.append(item.strip())
            elif isinstance(item, dict):
                text = item.get("name") or item.get("path") or item.get("text") or item.get("file")
                if text:
                    out.append(str(text).strip())
                else:
                    out.append(json.dumps(item, ensure_ascii=False))
            else:
                s = str(item).strip()
                if s:
                    out.append(s)
        return out
    if isinstance(value, str):
        s = value.strip()
        if not s:
            return []
        if s.startswith("["):
            try:
                return _as_str_list(json.loads(s))
            except json.JSONDecodeError:
                return [s]
        return [s]
    return [str(value)]


def args_to_retrieved_context(args: dict) -> RetrievedContext:
    """将工具参数规范化为 RetrievedContext。"""
    related = _as_str_list(args.get("related_tests"))
    callers = _as_str_list(args.get("caller_locations"))
    snippets_raw = args.get("similar_snippets") or []
    fixes_raw = args.get("similar_fixes") or []

    snippets: list[dict] = []
    for item in snippets_raw if isinstance(snippets_raw, list) else _as_str_list(snippets_raw):
        if isinstance(item, dict):
            snippets.append(item)
        elif isinstance(item, str) and item.strip():
            snippets.append({"text": item.strip()})

    fixes: list[dict] = []
    for item in fixes_raw if isinstance(fixes_raw, list) else _as_str_list(fixes_raw):
        if isinstance(item, dict):
            fixes.append(item)
        elif isinstance(item, str) and item.strip():
            fixes.append({"text": item.strip()})

    return RetrievedContext(
        related_tests=related,
        caller_locations=callers,
        similar_snippets=snippets,
        similar_fixes=fixes,
    )


def submit_retrieved_context(args: dict) -> str:
    """校验并序列化 RetrievedContext；空 related_tests 返回 Error（不触发终态）。"""
    ctx = args_to_retrieved_context(args if isinstance(args, dict) else {})
    if not ctx.related_tests:
        return (
            "Error: related_tests 不能为空。"
            "请先用 find_test/grep/search 收集测试路径，再调用本工具提交。"
        )
    return json.dumps(ctx.to_dict(), ensure_ascii=False)


def build_submit_retrieved_context_tool() -> dict:
    """注册表条目（terminal=True：成功后 AgentLoop 结束并以 payload 为 final）。"""
    return {
        "schema": auto_schema(SubmitRetrievedContextArgs),
        "args_dataclass": SubmitRetrievedContextArgs,
        "risky": False,
        "execution_tier": "host",
        "terminal": True,
        "description": (
            "检索结束时必须调用：提交 RetrievedContext。"
            "参数: related_tests(必填, string[])、caller_locations、"
            "similar_snippets、similar_fixes（后三者可选 string[]）。"
            "成功调用后任务结束，勿再输出散文 JSON。"
        ),
        "run": submit_retrieved_context,
    }
