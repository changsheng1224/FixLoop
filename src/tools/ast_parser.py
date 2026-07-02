"""AST 解析 Tool：用 Python stdlib ast 模块解析文件结构。

输出函数/类/方法的结构化信息，注释节点被排除以防止 Prompt 注入。
"""

import ast
from dataclasses import dataclass


@dataclass
class AstParseArgs:
    """ast_parse 参数。"""

    path: str  # 必填


def auto_schema_for(cls):
    """简化版 auto_schema（避免循环依赖 agent_runtime.schema_utils）。"""
    from dataclasses import MISSING, fields

    schema = {}
    for f in fields(cls):
        if f.default is not MISSING:
            schema[f.name] = f"{_type_name(f.type)}={f.default}"
        else:
            schema[f.name] = _type_name(f.type)
    return schema


def _type_name(t) -> str:
    return "str"


def ast_parse(context, args: dict) -> str:
    """解析 Python 文件为结构化函数/类/方法列表。

    Args:
        context: ToolContext 实例。
        args: 包含 'path' 字段的字典。

    Returns:
        JSON 字符串列表。
    """
    import json

    raw_path = args.get("path", "")
    if not raw_path:
        return "Error: 缺少必填参数 path"

    try:
        target = context.resolve(raw_path)
    except ValueError as e:
        return f"Error: {e}"

    if not target.is_file():
        return f"Error: 文件不存在: {raw_path}"

    try:
        source = target.read_text(encoding="utf-8")
        tree = ast.parse(source)
    except SyntaxError as e:
        return f"Error: 语法错误，无法解析: {e}"
    except Exception as e:
        return f"Error: {e}"

    results = []
    for node in ast.walk(tree):
        info = _extract_node(node)
        if info:
            results.append(info)

    return json.dumps(results, ensure_ascii=False, indent=2)


def _extract_node(node) -> dict | None:
    """从 AST 节点提取结构化信息。"""
    if isinstance(node, ast.FunctionDef):
        func_type = "function"
    elif isinstance(node, ast.AsyncFunctionDef):
        func_type = "async_function"
    elif isinstance(node, ast.ClassDef):
        func_type = "class"
    else:
        return None

    info = {
        "name": node.name,
        "type": func_type,
        "lineno": node.lineno,
        "end_lineno": node.end_lineno,
    }

    # 提取参数（仅函数/方法）
    if func_type in ("function", "async_function"):
        args = []
        for arg in node.args.args:
            annotation = ""
            if arg.annotation:
                try:
                    annotation = ast.unparse(arg.annotation)
                except Exception:
                    annotation = "unknown"
            args.append({"name": arg.arg, "annotation": annotation})
        info["args"] = args

    # 提取装饰器
    if node.decorator_list:
        decorators = []
        for d in node.decorator_list:
            try:
                decorators.append(ast.unparse(d))
            except Exception:
                decorators.append("@...")
        info["decorators"] = decorators

    # 提取 docstring 摘要
    doc = ast.get_docstring(node)
    if doc:
        info["docstring_summary"] = doc.strip().split("\n")[0][:100]

    return info
