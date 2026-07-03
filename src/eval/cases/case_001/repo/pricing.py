"""价格计算（含故意的类型转换 bug）。"""


def line_total(unit_price, count):
    """计算行总价。unit_price 可能来自 JSON 字符串。"""
    return unit_price + count  # BUG: 应为 int(unit_price) * count
