import pytest

from app.pagination import page_count, page_slice


def test_exact_multiple_needs_no_extra_page():
    assert page_count(10, 5) == 2


def test_partial_last_page_is_counted():
    # 10 items at 3 per page is 4 pages: 3 + 3 + 3 + 1
    assert page_count(10, 3) == 4


def test_no_items_means_no_pages():
    assert page_count(0, 5) == 0


def test_per_page_must_be_positive():
    with pytest.raises(ValueError):
        page_count(10, 0)


def test_page_slice_returns_the_requested_window():
    assert page_slice([1, 2, 3, 4, 5], page=2, per_page=2) == [3, 4]


def test_page_slice_rejects_page_zero():
    with pytest.raises(ValueError):
        page_slice([1, 2, 3], page=0, per_page=2)
