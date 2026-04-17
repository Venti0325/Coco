import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from src.utils import add, sub


def test_add():
    assert add(2, 3) == 5
    assert add(-1, 1) == 0


def test_sub():
    assert sub(5, 3) == 2
