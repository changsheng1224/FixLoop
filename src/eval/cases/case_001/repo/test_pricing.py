from pricing import line_total


def test_line_total_int():
    assert line_total(10, 3) == 30


def test_line_total_str_price():
    assert line_total("10", 3) == 30
