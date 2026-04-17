def score_a(x):
    v = x * 2
    if v < 0:
        v = 0
    elif v > 100:
        v = 100
    return v
