"""AST 解析 Tool：用 Python stdlib ast 模块解析文件结构。

输出函数/类/方法的结构化信息，注释节点被排除以防止 Prompt 注入。
"""

import ast
from dataclasses import dataclass


@dataclass
class AstParseArgs:
    """ast_parse 参数。"""

    path: str  # 必填
    start_line: int = 0  # 可选：局部分析起始行
    end_line: int = 0    # 可选：局部分析结束行


_DEFAULT_CONTEXT_LINES = 20


def ast_parse(context, args: dict) -> str:
    """解析 Python 文件为结构化函数/类/方法列表。

    支持局部分析：提供 start_line/end_line 时仅输出附近节点。

    Args:
        context: ToolContext 实例。
        args: 包含 'path' 字段的字典，可选 'start_line'/'end_line'。

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

    start_line = int(args.get("start_line", 0) or 0)
    end_line = int(args.get("end_line", 0) or 0)

    results = []
    for node in ast.walk(tree):
        info = _extract_node(node)
        if info:
            results.append(info)

    # 局部分析：仅保留 suspect 行附近的节点
    if start_line > 0 and end_line >= start_line:
        window_start = max(1, start_line - _DEFAULT_CONTEXT_LINES)
        window_end = end_line + _DEFAULT_CONTEXT_LINES
        results = [
            r for r in results
            if r["lineno"] <= window_end and (r["end_lineno"] or r["lineno"]) >= window_start
        ]

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


def check_semantic_equivalence(original: str, patched: str) -> dict:
    """对比两个 Python 源码的 AST 结构，检测语义漂移。

    Returns:
        {"status": "semantic_ok" | "drift", "detail": str}
    """
    result = {"status": "semantic_ok", "detail": ""}
    try:
        t1 = ast.parse(original)
        t2 = ast.parse(patched)
    except SyntaxError as e:
        return {"status": "drift", "detail": f"syntax error: {e}"}

    sigs1 = _extract_signatures(t1)
    sigs2 = _extract_signatures(t2)
    removed = sigs1 - sigs2
    added = sigs2 - sigs1
    if removed or added:
        parts = []
        if removed:
            parts.append(f"removed: {sorted(removed)}")
        if added:
            parts.append(f"added: {sorted(added)}")
        result["status"] = "drift"
        result["detail"] = "; ".join(parts)
    return result


def _extract_signatures(tree: ast.AST) -> set[str]:
    """提取 AST 中所有函数/类的方法签名。"""
    sigs = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef):
            params = ", ".join(a.arg for a in node.args.args)
            sigs.add(f"def {node.name}({params})")
        elif isinstance(node, ast.ClassDef):
            sigs.add(f"class {node.name}")
    return sigs
