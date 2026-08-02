import asyncio

from app.inventory import available, reserve_all


def test_single_reservation():
    assert asyncio.run(reserve_all(100, [5])) == 5


def test_concurrent_reservations_do_not_lose_updates():
    # Fifty concurrent single-unit reservations should reserve fifty units.
    assert asyncio.run(reserve_all(100, [1] * 50)) == 50


def test_reservation_beyond_stock_is_skipped():
    assert asyncio.run(reserve_all(3, [10])) == 0


def test_available_never_goes_negative():
    assert available(10, 25) == 0
