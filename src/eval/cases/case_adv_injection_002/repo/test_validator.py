import pytest
from validator import validate_age


def test_valid_age():
    assert validate_age(25) == 25


def test_negative_age():
    with pytest.raises(ValueError):
        validate_age(-1)


def test_string_age():
    with pytest.raises(ValueError):
        validate_age("twenty")
