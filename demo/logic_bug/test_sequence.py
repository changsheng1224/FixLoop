"""logic_bug demo 测试。test_iota_* 暴露 off-by-one。"""

from sequence import iota


def test_iota_three():
    assert iota(3) == [1, 2, 3]


def test_iota_one():
    assert iota(1) == [1]


def test_iota_zero():
    assert iota(0) == []
