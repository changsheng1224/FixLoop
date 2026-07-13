import pytest
from validator import validate_name


def test_normal_name():
    assert validate_name(" Alice ") == "Alice"


def test_empty_name_raises():
    with pytest.raises(ValueError):
        validate_name("")
