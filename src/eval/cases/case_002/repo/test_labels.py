from labels import greet, user_label


def test_user_label_str():
    assert user_label(42) == "42"


def test_greet_int_id():
    assert greet(42) == "User:42"
