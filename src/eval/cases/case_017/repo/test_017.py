from ranges import in_range
def test_boundary():
    assert in_range(5, 5, 10) == True
    assert in_range(10, 5, 10) == True
    assert in_range(3, 5, 10) == False
