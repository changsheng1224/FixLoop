"""安全拼接（含故意的 None 未判断 bug）。"""


def safe_concat(a, b):
    """拼接两段文本；None 应视为空字符串。"""
    return a + b  # BUG: a 或 b 为 None 时 TypeError
