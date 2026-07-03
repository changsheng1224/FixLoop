from users import Profile, User, display_name


def test_display_name_with_profile():
    user = User(name="alice", profile=Profile(display_name="Alice"))
    assert display_name(user) == "Alice"


def test_display_name_fallback_to_name():
    user = User(name="bob", profile=None)
    assert display_name(user) == "bob"
