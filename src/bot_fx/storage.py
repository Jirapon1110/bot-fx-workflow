"""SQLite persistence. Re-running the workflow appends only rows not stored yet."""

from __future__ import annotations

import sqlite3
from collections.abc import Iterable
from datetime import UTC, datetime
from pathlib import Path

from .client import DailyRate

CURRENCY_PAIR = "THB/USD"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS daily_ref_rate (
    period        TEXT PRIMARY KEY,           -- ISO date, one row per business day
    rate          REAL NOT NULL,              -- Baht per 1 USD
    currency_pair TEXT NOT NULL,
    fetched_at    TEXT NOT NULL               -- UTC ISO timestamp of the run that inserted it
)
"""

# DO NOTHING only skips duplicate periods; unlike "INSERT OR IGNORE" it still raises on
# NOT NULL violations. Consequence: a day already stored is never revised (append-only).
_INSERT = (
    "INSERT INTO daily_ref_rate (period, rate, currency_pair, fetched_at) "
    "VALUES (?, ?, ?, ?) ON CONFLICT(period) DO NOTHING"
)


def append_rates(db_path: Path, rates: Iterable[DailyRate]) -> int:
    """Append rates, skipping periods already present. Returns rows inserted."""
    db_path.parent.mkdir(parents=True, exist_ok=True)
    fetched_at = datetime.now(UTC).isoformat(timespec="seconds")
    rows = [(r.period.isoformat(), r.rate, CURRENCY_PAIR, fetched_at) for r in rates]

    # sqlite3's context manager commits/rolls back but does not close.
    conn = sqlite3.connect(db_path)
    try:
        with conn:
            conn.execute(_SCHEMA)
            before = conn.total_changes
            conn.executemany(_INSERT, rows)
            return conn.total_changes - before
    finally:
        conn.close()
