def score_b(x):
    v = x + 10
    if v < 0:
        v = 0
    elif v > 100:
        v = 100
    return v
