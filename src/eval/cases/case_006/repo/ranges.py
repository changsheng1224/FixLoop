"""整数区间（含故意的 off-by-one bug）。"""


def inclusive_range(start: int, end: int) -> list[int]:
    """返回闭区间 [start, end] 的整数列表。"""
    return list(range(start, end))  # BUG: 缺少 end + 1
