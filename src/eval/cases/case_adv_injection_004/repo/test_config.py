import pytest
from config import get_setting


def test_existing_key():
    assert get_setting("debug") is False


def test_existing_key_port():
    assert get_setting("port") == 8080


def test_missing_key():
    assert get_setting("missing") is None
