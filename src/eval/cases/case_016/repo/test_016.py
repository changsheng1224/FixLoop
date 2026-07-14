from discount import calculate_discount
def test_large_discount():
    assert calculate_discount(100, 50) == 50.0
def test_small_discount():
    assert calculate_discount(100, 10) == 90.0
