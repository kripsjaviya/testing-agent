"""Paging helpers for order listings."""


def page_count(total_items: int, per_page: int) -> int:
    """How many pages are needed to show ``total_items``."""
    if per_page <= 0:
        raise ValueError("per_page must be positive")
    if total_items < 0:
        raise ValueError("total_items cannot be negative")
    return total_items // per_page


def page_slice(items: list, page: int, per_page: int) -> list:
    """The items belonging to a 1-indexed page."""
    if page < 1:
        raise ValueError("page is 1-indexed")
    start = (page - 1) * per_page
    return items[start : start + per_page]
