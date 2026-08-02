"""Turning internal records into API responses."""

ORDER_FIELDS = ("id", "customer", "quantity", "unit_price", "total", "status")


def serialise_order(order: dict) -> dict:
    """Render an order for an API response."""
    return {
        "id": order["id"],
        "customer": order["customer"],
        "quantity": order["quantity"],
        "unit_price": order["unit_price"],
        "total": order["total"],
    }


def serialise_many(orders: list[dict]) -> list[dict]:
    """Render a list of orders."""
    return [serialise_order(order) for order in orders]
