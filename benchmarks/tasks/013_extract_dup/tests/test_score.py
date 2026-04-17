from src.a import score_a
from src.b import score_b


def test_a_bounds():
    assert score_a(-1) == 0
    assert score_a(200) == 100
    assert score_a(10) == 20


def test_b_bounds():
    assert score_b(-100) == 0
    assert score_b(200) == 100
    assert score_b(5) == 15
