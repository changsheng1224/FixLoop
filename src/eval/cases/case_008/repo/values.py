"""数值裁剪。"""


def clamp(value: int, lo: int, hi: int) -> int:
    return max(lo, min(hi, value))
