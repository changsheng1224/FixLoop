"""Java AST 解析工具（javalang 纯 Python 实现）。"""

from dataclasses import dataclass

from agent_runtime.tool_result import ToolResult


@dataclass
class JavaAstParseArgs:
    path: str  # 必填


def java_ast_parse(context, args: dict) -> ToolResult:
    """解析 Java 文件为结构化类/方法列表。"""
    import javalang

    raw_path = args.get("path", "")
    if not raw_path:
        return ToolResult.error("Error: 缺少必填参数 path")
    try:
        target = context.resolve(raw_path)
    except ValueError as e:
        return ToolResult.error(f"Error: {e}")
    if not target.is_file():
        return ToolResult.error(f"Error: 文件不存在: {raw_path}")
    try:
        tree = javalang.parse.parse(target.read_text(encoding="utf-8"))
    except javalang.parser.JavaSyntaxError as e:
        return ToolResult.error(f"Error: Java 语法错误: {e}")
    except Exception as e:
        return ToolResult.error(f"Error: 解析失败: {e}")

    lines = []
    # 类型定义
    if tree.types:
        for typ in tree.types:
            kind = "interface" if getattr(typ, "interface", False) else "class"
            lines.append(f"[{kind}] {typ.name} (line {typ.position.line if typ.position else '?'})")
            # 方法
            for member in getattr(typ, "body", []) or []:
                if isinstance(member, javalang.tree.MethodDeclaration):
                    params = ", ".join(
                        f"{p.type.name} {p.name}" for p in getattr(member, "parameters", []) or []
                    )
                    return_type = member.return_type.name if member.return_type else "void"
                    line_no = member.position.line if member.position else "?"
                    lines.append(
                        f"  method: {member.name}({params}) → {return_type} (line {line_no})"
                    )
    if not lines:
        return ToolResult(content="(无结构)")
    return ToolResult(content="\n".join(lines))
