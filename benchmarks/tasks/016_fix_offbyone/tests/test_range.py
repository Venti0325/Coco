from src.range_sum import range_sum


def test_basic():
    assert range_sum(1, 5) == 15  # 1+2+3+4+5
    assert range_sum(0, 0) == 0
    assert range_sum(1, 3) == 6
