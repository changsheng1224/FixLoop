"""分数归一化（hop 2）。"""

from values import clamp


def normalize_score(score: int) -> float:
    """输入已是 0–100 的百分制分数，返回同尺度浮点值。"""
    return clamp(score, 0, 100) / 100  # BUG: 不应再除以 100
