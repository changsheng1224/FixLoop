from __future__ import annotations


def normalize_patch_text_field(value: object) -> str:
    """将 patch 行字段规范为源码文本（E19）。

    根因：模型常把 ``original_lines``/``patched_lines`` 输出成 JSON list，
    或把 list 的 ``repr`` 当成字符串 → 导出出现 ``-['...']``，无法对齐源码。
    """
    if value is None:
        return ""
    if isinstance(value, list):
        parts: list[str] = []
        for item in value:
            if item is None:
                continue
            parts.append(str(item).rstrip("\r\n"))
        return "\n".join(parts)
    if isinstance(value, tuple):
        return normalize_patch_text_field(list(value))
    if not isinstance(value, str):
        return str(value)
    text = value
    stripped = text.strip()
    if len(stripped) >= 2 and stripped[0] == "[" and stripped[-1] == "]":
        try:
            import ast

            lit = ast.literal_eval(stripped)
            if isinstance(lit, list):
                return normalize_patch_text_field(lit)
        except (SyntaxError, ValueError, MemoryError):
            pass
    return text
