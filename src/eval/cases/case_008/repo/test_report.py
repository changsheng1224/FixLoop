from report import average_percent


def test_average_percent():
    assert average_percent([80, 90]) == 85.0


def test_average_percent_empty():
    assert average_percent([]) == 0.0
