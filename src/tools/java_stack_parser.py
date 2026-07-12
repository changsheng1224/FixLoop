"""Java 异常堆栈解析工具。"""

import re
from dataclasses import dataclass


@dataclass
class JavaStackParseArgs:
    traceback: str  # 必填


def java_stack_parse(context, args: dict) -> str:
    """解析 Java 异常堆栈为结构化数据。"""
    traceback = args.get("traceback", "")
    if not traceback:
        return "Error: 缺少必填参数 traceback"

    lines = []
    # 异常类型
    exc_match = re.search(r"(\w+(?:Error|Exception))(?::\s*(.*))?", traceback)
    if exc_match:
        lines.append(f"Exception: {exc_match.group(1)}")
        if exc_match.group(2):
            lines.append(f"Message: {exc_match.group(2)}")
        lines.append("")

    # 调用栈
    for match in re.finditer(r"at\s+([\w.$]+)\((\w+\.java):(\d+)\)", traceback):
        lines.append(f"Frame: {match.group(1)} @ {match.group(2)}:{match.group(3)}")

    # root cause
    caused = re.search(r"Caused by:\s*(\w+(?:Error|Exception))(?::\s*(.*))?", traceback)
    if caused:
        lines.append("")
        lines.append(f"Caused by: {caused.group(1)}")
        if caused.group(2):
            lines.append(f"  Message: {caused.group(2)}")

    return "\n".join(lines) if lines else "(无法解析)"
