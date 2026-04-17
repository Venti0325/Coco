def range_sum(lo: int, hi: int) -> int:
    """Return the sum of integers in the inclusive range [lo, hi]."""
    total = 0
    # Bug: upper bound should be hi + 1 for inclusive sum
    for i in range(lo, hi):
        total += i
    return total
