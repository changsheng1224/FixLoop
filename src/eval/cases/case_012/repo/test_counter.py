from counter import count_words

def test_count_words():
    assert count_words("hello world") == 2

def test_empty():
    assert count_words("") == 0
