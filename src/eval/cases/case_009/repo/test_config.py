from config_loader import load_multiplier


def test_multiplier():
    assert load_multiplier() == 2
