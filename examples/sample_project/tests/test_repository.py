import pytest

from app.repository import connect, find_by_customer, insert_order, total_for_customer


@pytest.fixture()
def db():
    connection = connect()
    insert_order(connection, "ana", 30.0)
    insert_order(connection, "ana", 12.0)
    insert_order(connection, "bruno", 99.0)
    yield connection
    connection.close()


def test_orders_are_scoped_to_one_customer(db):
    assert len(find_by_customer(db, "ana")) == 2


def test_totals_are_summed_per_customer(db):
    assert total_for_customer(db, "ana") == 42.0


def test_unknown_customer_has_no_orders(db):
    assert find_by_customer(db, "nobody") == []


def test_crafted_customer_name_cannot_read_other_rows(db):
    # A customer name is untrusted input and must never alter the query.
    assert find_by_customer(db, "' OR '1'='1") == []


def test_customer_names_with_apostrophes_are_supported(db):
    insert_order(db, "o'brien", 5.0)
    assert len(find_by_customer(db, "o'brien")) == 1
