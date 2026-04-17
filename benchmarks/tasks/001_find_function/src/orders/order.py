from dataclasses import dataclass


@dataclass
class Order:
    id: int
    amount: float


def total_of(orders: list[Order]) -> float:
    return sum(o.amount for o in orders)
