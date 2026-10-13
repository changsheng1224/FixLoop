"""Explicit API protection; definition changes alone do not prove semantic drift."""

import ast


def interfaces(source):
    result = {}

    def visit(nodes, prefix=""):
        for node in nodes:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                name = prefix + node.name
                if isinstance(node, ast.ClassDef):
                    signature = (
                        "class",
                        tuple(ast.dump(b) for b in node.bases),
                        tuple(ast.dump(k) for k in node.keywords),
                    )
                else:
                    signature = (
                        type(node).__name__,
                        ast.dump(node.args),
                        ast.dump(node.returns) if node.returns else "",
                    )
                result[name] = (signature, tuple(ast.dump(d) for d in node.decorator_list))
                visit(node.body, name + ".")

    visit(ast.parse(source).body)
    return result


def protected_interface_changes(original, patched, protected):
    if not protected:
        return []
    try:
        before = interfaces(original)
    except SyntaxError:
        return []  # No original signature could be established.
    try:
        after = interfaces(patched)
    except SyntaxError:
        return [name for name in protected if name in before]
    return [name for name in protected if name in before and before[name] != after.get(name)]
