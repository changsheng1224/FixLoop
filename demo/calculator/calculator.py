"""计算器模块（含一个故意的 TypeError bug）。"""


def add(a, b):
    """返回 a + b。"""
    return a + b  # BUG: 当 a 或 b 为 str 时会 TypeError


def subtract(a, b):
    """返回 a - b。"""
    return a - b


def multiply(a, b):
    """返回 a * b。"""
    return a * b
