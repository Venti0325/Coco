"""Tax-related utilities for billing."""


TAX_RATE = 0.08


def compute_tax(amount: float, rate: float = TAX_RATE) -> float:
    """Return the tax portion of ``amount`` given ``rate``."""
    return round(amount * rate, 2)


def compute_total(amount: float, rate: float = TAX_RATE) -> float:
    return amount + compute_tax(amount, rate)
