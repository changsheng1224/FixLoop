"""模型输出解析策略（从 Agent.parse() 提取）。

支持 6 种解析格式：XML JSON tool / XML attr tool / final / function_calls / raw JSON / markdown。
"""

from __future__ import annotations

import json
import re


class ModelOutputParser:
    """模型输出解析器：从 raw text 提取 (kind, payload)。"""

    @staticmethod
    def parse(raw: str) -> tuple[str, dict | str]:
        """解析模型输出，提取结构化决策。

        支持两种工具调用格式 + 最终答案：
        - ``<tool>{"name":"x","args":{...}}</tool>`` → ("tool", payload_dict)
        - ``<tool name="x" path="f.py">body</tool>`` → ("tool", payload_dict)
        - ``<final>text</final>`` → ("final", text)
        - 格式不匹配 → ("retry", notice_text)
        """
        raw = raw.strip()

        from agent_runtime.parse_recovery import failure_from_json_in_tool, make_parse_retry

        if not raw:
            return ("retry", make_parse_retry(raw))

        # 尝试匹配 <final>...</final>
        final_match = re.search(r"<final>(.*?)</final>", raw, re.DOTALL)
        if final_match:
            return ("final", final_match.group(1).strip())

        # 尝试匹配 JSON 函数调用格式
        if raw.startswith("{"):
            try:
                data = json.loads(raw)
                tool_keys = ("action", "name", "tool", "function")
                if isinstance(data, dict) and any(k in data for k in tool_keys):
                    tool_name = (
                        data.get("action")
                        or data.get("name")
                        or data.get("tool")
                        or data.get("function")
                    )
                    tool_args = (
                        data.get("arguments") or data.get("args") or data.get("parameters") or {}
                    )
                    if isinstance(tool_name, str) and tool_name:
                        return ("tool", {"name": tool_name, "args": tool_args})
            except json.JSONDecodeError:
                pass

        # 模型直接输出了 JSON 答案 → 视为 final
        if (raw.startswith("[") or raw.startswith("{")) and not raw.startswith("<tool"):
            try:
                json.loads(raw)
            except json.JSONDecodeError:
                pass
            else:
                return ("final", raw)

        # 模型用 markdown 代码块包裹 JSON
        md_start = re.search(r"```(?:json)?\s*", raw)
        if md_start:
            content = raw[md_start.end() :]
            md_end = content.rfind("```")
            if md_end >= 0:
                inner = content[:md_end].strip()
                if inner.startswith("{") or inner.startswith("["):
                    return ("final", inner)

        # 尝试匹配 <function_calls> 格式 (DeepSeek native)
        fc_match = re.search(
            r"<function_calls>\s*(.*?)\s*</function_calls>",
            raw,
            re.DOTALL,
        )
        if fc_match:
            inner = fc_match.group(1)
            invokes = re.findall(
                r"<invoke\s+name=\"(\w+)\">(.*?)</invoke>",
                inner,
                re.DOTALL,
            )
            if invokes:
                name = invokes[0][0]
                params_str = invokes[0][1]
                args = {}
                for param_m in re.finditer(
                    r'<parameter\s+name="(\w+)">(.*?)</parameter>',
                    params_str,
                    re.DOTALL,
                ):
                    args[param_m.group(1)] = param_m.group(2).strip()
                return ("tool", {"name": name, "args": args})

        # 尝试匹配 JSON 格式 <tool>{...}</tool>
        from agent_runtime.text_tags import extract_between_tags

        json_match = extract_between_tags(raw, "<tool>", "</tool>")
        if json_match:
            try:
                payload = json.loads(json_match)
                return ("tool", payload)
            except json.JSONDecodeError as exc:
                return (
                    "retry",
                    make_parse_retry(raw, failure_from_json_in_tool(json_match, exc)),
                )

        # 尝试匹配 XML 属性格式
        tool_xml_match = re.search(
            r'<tool\s+name="([^"]+)"(.*?)>(.*?)</tool>',
            raw,
            re.DOTALL,
        )
        if tool_xml_match:
            name = tool_xml_match.group(1)
            attrs = tool_xml_match.group(2).strip()
            body = tool_xml_match.group(3).strip()
            return ("tool", {"name": name, "attrs": attrs, "body": body})

        # 都不匹配 → recovery prompt
        return ("retry", make_parse_retry(raw))
