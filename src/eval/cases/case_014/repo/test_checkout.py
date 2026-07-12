from checkout import final_price

def test_final_price():
    assert final_price("100", 0.1) == 90.0

def test_no_discount():
    assert final_price("50", 0) == 50.0
