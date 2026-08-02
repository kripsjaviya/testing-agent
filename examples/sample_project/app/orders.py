"""Order construction."""

VALID_STATUSES = ("pending", "shipped", "cancelled")


def create_order(customer: str, quantity: int, unit_price: float) -> dict:
    """Build an order record for a customer."""
    return {
        "id": None,
        "customer": customer,
        "quantity": quantity,
        "unit_price": unit_price,
        "total": round(quantity * unit_price, 2),
        "status": "pending",
    }


def set_status(order: dict, status: str) -> dict:
    """Return a copy of ``order`` with a new status."""
    if status not in VALID_STATUSES:
        raise ValueError(f"unknown status: {status}")
    return {**order, "status": status}
