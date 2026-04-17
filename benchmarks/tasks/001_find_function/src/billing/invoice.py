from .tax import compute_total


def render_invoice(customer: str, amount: float) -> str:
    total = compute_total(amount)
    return f"Invoice for {customer}: ${total:.2f}"
