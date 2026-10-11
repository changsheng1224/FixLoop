"""Pure Python structure extraction shared by both runtime layers."""

from __future__ import annotations

import ast


def python_structure(source: str, *, start_line: int = 0, end_line: int = 0) -> list[dict]:
    tree = ast.parse(source)
    results = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef):
            continue
        kind = (
            "class"
            if isinstance(node, ast.ClassDef)
            else "async_function"
            if isinstance(node, ast.AsyncFunctionDef)
            else "function"
        )
        info = {
            "name": node.name,
            "type": kind,
            "lineno": node.lineno,
            "end_lineno": node.end_lineno,
        }
        if kind != "class":
            info["args"] = [
                {
                    "name": arg.arg,
                    "annotation": ast.unparse(arg.annotation) if arg.annotation else "",
                }
                for arg in node.args.args
            ]
        if node.decorator_list:
            info["decorators"] = [ast.unparse(decorator) for decorator in node.decorator_list]
        doc = ast.get_docstring(node)
        if doc:
            info["docstring_summary"] = doc.strip().split("\n")[0][:100]
        if start_line > 0 and end_line >= start_line:
            if info["lineno"] > end_line + 20 or (info["end_lineno"] or info["lineno"]) < max(
                1, start_line - 20
            ):
                continue
        results.append(info)
    return results
