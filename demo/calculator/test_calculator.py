"""计算器测试。test_add_str 是故意的失败测试。"""

from calculator import add, multiply, subtract


def test_add():
    assert add(3, 2) == 5


def test_add_str():
    """故意触发 TypeError：str + int。"""
    assert add("3", 2) == 5  # BUG: 没有类型转换


def test_subtract():
    assert subtract(5, 3) == 2


def test_multiply():
    assert multiply(4, 3) == 12
