from gateway import invoke


def test_invoke_mixed_types():
    assert invoke("2", 3) == 5
