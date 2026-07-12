from worker import compute
def test_compute():
    assert compute(41) == 42
def test_compute_none():
    assert compute(None) == 0
