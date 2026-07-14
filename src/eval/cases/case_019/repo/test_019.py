from checkout import apply_tax
def test_normal_cart():
    assert apply_tax({"a": 10}, 0.1) == 11.0
def test_none_cart():
    assert apply_tax(None, 0.1) == 0
