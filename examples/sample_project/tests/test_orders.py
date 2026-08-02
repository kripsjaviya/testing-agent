import pytest

from app.orders import create_order, set_status


def test_order_totals_are_computed():
    order = create_order("ana", 3, 10.0)
    assert order["total"] == 30.0


def test_order_starts_pending():
    assert create_order("ana", 1, 5.0)["status"] == "pending"


def test_negative_quantity_is_rejected():
    with pytest.raises(ValueError):
        create_order("ana", -3, 10.0)


def test_status_can_be_changed():
    order = set_status(create_order("ana", 1, 5.0), "shipped")
    assert order["status"] == "shipped"


def test_unknown_status_is_rejected():
    with pytest.raises(ValueError):
        set_status(create_order("ana", 1, 5.0), "teleported")
