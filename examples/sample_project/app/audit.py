"""Append-only audit log."""

from __future__ import annotations

from pathlib import Path


def append_entry(path: str | Path, message: str) -> None:
    """Record one audit line."""
    with open(path, "a", encoding="utf-8") as handle:
        handle.write(message.rstrip("\n") + "\n")


def count_entries(path: str | Path) -> int:
    """How many entries the audit log holds."""
    handle = open(path, encoding="utf-8")
    return sum(1 for line in handle if line.strip())
