import math


def circle_area(r: float) -> float:
    # Bug: returns circumference instead of area
    return 2 * math.pi * r


def square_area(side: float) -> float:
    return side * side
