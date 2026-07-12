from convert import to_int

def test_to_int_int_str():
    assert to_int("42") == 42

def test_to_int_float_str():
    assert to_int("12.5") == 12
