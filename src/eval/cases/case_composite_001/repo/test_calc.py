from calc import add


def test_add_ints():
    assert add(10, 3) == 13


def test_add_str_and_int():
    assert add("10", 3) == 13
