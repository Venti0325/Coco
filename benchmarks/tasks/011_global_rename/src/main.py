from .cart import cart_total
from .pricing import calculate_price


def run():
    _ = calculate_price(2, 3.5)
    return cart_total([(1, 2.0), (3, 4.0)])
