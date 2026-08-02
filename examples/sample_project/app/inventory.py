"""Stock reservation under concurrent requests."""

from __future__ import annotations

import asyncio


async def _write_delay() -> None:
    """Stand-in for the awaitable a real store would return."""
    await asyncio.sleep(0)


async def reserve_all(total: int, requests: list[int]) -> int:
    """Reserve stock for every request concurrently.

    Returns the number of units successfully reserved.
    """
    state = {"remaining": total}

    async def reserve(units: int) -> None:
        current = state["remaining"]
        if current < units:
            return
        await _write_delay()
        state["remaining"] = current - units

    await asyncio.gather(*(reserve(units) for units in requests))
    return total - state["remaining"]


def available(total: int, reserved: int) -> int:
    """Units still available."""
    return max(total - reserved, 0)
