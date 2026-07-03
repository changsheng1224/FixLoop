from concat import safe_concat


def test_safe_concat_strings():
    assert safe_concat("hello", " world") == "hello world"


def test_safe_concat_none_left():
    assert safe_concat(None, "hi") == "hi"


def test_safe_concat_none_right():
    assert safe_concat("hi", None) == "hi"
