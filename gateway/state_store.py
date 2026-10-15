"""SQLite WAL state store: idempotency, deduplication, atomic state machine."""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Iterator, Optional

from .config import (
    DEFAULT_BUSY_TIMEOUT_MS,
    DEFAULT_DB_CONNECT_TIMEOUT_SECONDS,
    DEFAULT_DISPATCH_BATCH_LIMIT,
)

STATUSES = ("RECEIVED", "VALIDATED", "DISPATCHED", "DELIVERED", "FAILED")

ALLOWED_TRANSITIONS: dict[str, set[str]] = {
    "RECEIVED": {"VALIDATED"},
    "VALIDATED": {"DISPATCHED"},
    "DISPATCHED": {"DELIVERED", "FAILED"},
    "DELIVERED": set(),
    "FAILED": {"VALIDATED"},
}

DDL_EVENTS = """
CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    idempotency_key TEXT NOT NULL UNIQUE,
    payload TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('RECEIVED','VALIDATED','DISPATCHED','DELIVERED','FAILED')),
    attempt_count INTEGER NOT NULL DEFAULT 0,
    last_error TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
"""

DDL_INDEX = "CREATE INDEX IF NOT EXISTS idx_events_status ON events(status);"


@dataclass
class EventRow:
    id: int
    idempotency_key: str
    payload: str
    status: str
    attempt_count: int
    last_error: Optional[str]
    created_at: str
    updated_at: str


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def connect(
    db_path: str,
    *,
    busy_timeout_ms: int = DEFAULT_BUSY_TIMEOUT_MS,
    timeout: float = DEFAULT_DB_CONNECT_TIMEOUT_SECONDS,
) -> sqlite3.Connection:
    conn = sqlite3.connect(
        db_path, timeout=timeout, isolation_level=None, check_same_thread=False
    )
    conn.row_factory = sqlite3.Row
    conn.execute(f"PRAGMA busy_timeout={int(busy_timeout_ms)};")
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA synchronous=NORMAL;")
    return conn


def init_db(
    db_path: str,
    *,
    busy_timeout_ms: int = DEFAULT_BUSY_TIMEOUT_MS,
    timeout: float = DEFAULT_DB_CONNECT_TIMEOUT_SECONDS,
) -> sqlite3.Connection:
    conn = connect(db_path, busy_timeout_ms=busy_timeout_ms, timeout=timeout)
    conn.execute(DDL_EVENTS)
    conn.execute(DDL_INDEX)
    return conn


@contextmanager
def transaction(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise


def _row_to_event(row: sqlite3.Row) -> EventRow:
    return EventRow(
        id=row["id"],
        idempotency_key=row["idempotency_key"],
        payload=row["payload"],
        status=row["status"],
        attempt_count=row["attempt_count"],
        last_error=row["last_error"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


def get_event(conn: sqlite3.Connection, event_id: int) -> EventRow | None:
    row = conn.execute("SELECT * FROM events WHERE id=?", (event_id,)).fetchone()
    return _row_to_event(row) if row is not None else None


def insert_event(
    conn: sqlite3.Connection,
    idempotency_key: str,
    payload: str,
    status: str = "RECEIVED",
) -> tuple[int | None, bool]:
    now = _now_iso()
    with transaction(conn):
        cursor = conn.execute(
            "INSERT INTO events (idempotency_key, payload, status, attempt_count, created_at, updated_at) "
            "VALUES (?, ?, ?, 0, ?, ?) "
            "ON CONFLICT(idempotency_key) DO NOTHING",
            (idempotency_key, payload, status, now, now),
        )
        created = cursor.rowcount == 1
        if created:
            return cursor.lastrowid, True
        row = conn.execute(
            "SELECT id FROM events WHERE idempotency_key=?", (idempotency_key,)
        ).fetchone()
        return (row["id"] if row is not None else None), False


def transition_status(
    conn: sqlite3.Connection,
    event_id: int,
    from_status: str,
    to_status: str,
) -> bool:
    if to_status not in ALLOWED_TRANSITIONS.get(from_status, set()):
        return False
    with transaction(conn):
        cursor = conn.execute(
            "UPDATE events SET status=?, updated_at=? WHERE id=? AND status=?",
            (to_status, _now_iso(), event_id, from_status),
        )
        return cursor.rowcount == 1


def record_attempt(
    conn: sqlite3.Connection,
    event_id: int,
    attempt_count: int,
    last_error: str | None = None,
) -> None:
    with transaction(conn):
        conn.execute(
            "UPDATE events SET attempt_count=?, last_error=?, updated_at=? WHERE id=?",
            (attempt_count, last_error, _now_iso(), event_id),
        )


def fetch_dispatchable(
    conn: sqlite3.Connection, limit: int = DEFAULT_DISPATCH_BATCH_LIMIT
) -> list[EventRow]:
    rows = conn.execute(
        "SELECT * FROM events WHERE status IN ('VALIDATED','FAILED') "
        "ORDER BY created_at, id LIMIT ?",
        (limit,),
    ).fetchall()
    return [_row_to_event(row) for row in rows]


def resend_failed(conn: sqlite3.Connection, event_id: int) -> bool:
    """Return a FAILED event to the delivery queue without re-validation."""
    return transition_status(conn, event_id, "FAILED", "VALIDATED")