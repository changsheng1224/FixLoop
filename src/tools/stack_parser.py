"""堆栈解析 Tool：正则解析 Python Traceback。

支持链式异常（During handling of the above exception...）
和 SyntaxError 特殊格式。
"""

import re
from dataclasses import dataclass


@dataclass
class StackParseArgs:
    traceback: str  # 必填


def stack_parse(context, args: dict) -> str:
    """解析 Python Traceback 为结构化数据。

    Args:
        context: ToolContext 实例（未使用，保持签名一致）。
        args: 包含 'traceback' 字段的字典。

    Returns:
        JSON 字符串。
    """
    import json

    traceback_text = args.get("traceback", "")
    if not traceback_text:
        return "Error: 缺少必填参数 traceback"

    result = {
        "exception_type": "",
        "exception_message": "",
        "frames": [],
    }

    # 提取异常类型和消息
    exc_match = re.search(
        r"(\w+(?:Error|Exception|Warning))\s*:?\s*(.*)",
        traceback_text,
    )
    if exc_match:
        result["exception_type"] = exc_match.group(1)
        result["exception_message"] = exc_match.group(2).strip()

    # 提取调用栈帧
    frame_pattern = r'File\s+"([^"]+)",\s*line\s+(\d+),\s*in\s+(\w+)'
    for m in re.finditer(frame_pattern, traceback_text):
        result["frames"].append(
            {
                "file": m.group(1),
                "line": int(m.group(2)),
                "function": m.group(3),
            }
        )

    # 链式异常检测
    result["is_chained"] = "During handling of the above exception" in traceback_text

    # SyntaxError 特殊处理
    if result["exception_type"] == "SyntaxError":
        syntax_match = re.search(
            r"SyntaxError:.*\((.+?),\s*line\s*(\d+)\)",
            traceback_text,
        )
        if syntax_match:
            result["syntax_file"] = syntax_match.group(1)
            result["syntax_line"] = int(syntax_match.group(2))

    return json.dumps(result, ensure_ascii=False, indent=2)
