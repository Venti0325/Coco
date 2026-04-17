import math
from src.shape import circle_area, square_area


def test_circle():
    assert math.isclose(circle_area(1.0), math.pi, rel_tol=1e-6)
    assert math.isclose(circle_area(2.0), 4 * math.pi, rel_tol=1e-6)


def test_square():
    assert square_area(3) == 9
