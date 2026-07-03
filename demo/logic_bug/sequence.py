"""序列工具（含一个故意的 off-by-one bug）。"""


def iota(n: int) -> list[int]:
    """返回 [1, 2, ..., n]。n <= 0 时返回空列表。"""
    if n <= 0:
        return []
    return list(range(1, n))  # BUG: 应为 range(1, n + 1)
