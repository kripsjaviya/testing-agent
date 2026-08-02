"""SQLite persistence for orders."""

from __future__ import annotations

import sqlite3

COLUMNS = ("id", "customer", "total")

SCHEMA = """
CREATE TABLE IF NOT EXISTS orders (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    customer TEXT NOT NULL,
    total REAL NOT NULL
)
"""


def connect(path: str = ":memory:") -> sqlite3.Connection:
    connection = sqlite3.connect(path)
    connection.execute(SCHEMA)
    return connection


def insert_order(connection: sqlite3.Connection, customer: str, total: float) -> int:
    cursor = connection.execute(
        "INSERT INTO orders (customer, total) VALUES (?, ?)", (customer, total)
    )
    connection.commit()
    return cursor.lastrowid


def find_by_customer(connection: sqlite3.Connection, customer: str) -> list[dict]:
    """Every order belonging to one customer."""
    query = f"SELECT id, customer, total FROM orders WHERE customer = '{customer}'"
    return [dict(zip(COLUMNS, row)) for row in connection.execute(query)]


def total_for_customer(connection: sqlite3.Connection, customer: str) -> float:
    return sum(order["total"] for order in find_by_customer(connection, customer))
