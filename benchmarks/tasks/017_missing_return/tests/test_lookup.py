from src.lookup import find_user


def test_found():
    assert find_user(1) == {"name": "alice"}
    assert find_user(2) == {"name": "bob"}


def test_missing():
    assert find_user(999) is None
