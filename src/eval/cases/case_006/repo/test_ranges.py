from ranges import inclusive_range


def test_inclusive_range_basic():
    assert inclusive_range(1, 3) == [1, 2, 3]


def test_inclusive_range_single():
    assert inclusive_range(5, 5) == [5]
