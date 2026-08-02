from app.orders import create_order
from app.serialisers import ORDER_FIELDS, serialise_many, serialise_order


def order():
    return {**create_order("ana", 2, 12.5), "id": 7}


def test_serialised_order_keeps_the_customer():
    assert serialise_order(order())["customer"] == "ana"


def test_serialised_order_keeps_the_total():
    assert serialise_order(order())["total"] == 25.0


def test_serialised_order_exposes_every_field():
    assert set(serialise_order(order())) == set(ORDER_FIELDS)


def test_serialise_many_preserves_order():
    records = [{**order(), "id": 1}, {**order(), "id": 2}]
    assert [row["id"] for row in serialise_many(records)] == [1, 2]


def test_serialise_many_handles_an_empty_list():
    assert serialise_many([]) == []
