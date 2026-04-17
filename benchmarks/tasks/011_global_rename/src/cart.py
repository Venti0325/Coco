from .pricing import calculate_price


def cart_total(items):
    return sum(calculate_price(q, u) for q, u in items)
