"""成绩汇总（hop 3）。"""

from transform import normalize_score


def average_percent(scores: list[int]) -> float:
    if not scores:
        return 0.0
    return sum(normalize_score(s) for s in scores) / len(scores)
