"""VM-local SQLite (WAL) storage shared by ``OrderJournal`` and ``SequenceLog``.

A single connection, guarded by a lock, so the worker thread and the async
API layer can both use it safely. ``transaction()`` is how a caller composes
an atomic multi-table write (e.g. updating the journal and appending a
sequence-log row together) — the "share a local transaction" requirement in
plan §114.
"""

import os
import sqlite3
import stat
import threading
from collections.abc import Generator
from contextlib import contextmanager

_OWNER_READ_WRITE = stat.S_IRUSR | stat.S_IWUSR

_SCHEMA = """
CREATE TABLE IF NOT EXISTS order_journal (
    client_order_id TEXT PRIMARY KEY,
    payload_hash TEXT NOT NULL,
    symbol TEXT NOT NULL,
    side TEXT NOT NULL,
    order_type TEXT NOT NULL,
    volume TEXT NOT NULL,
    price TEXT,
    notional_price TEXT,
    time_in_force TEXT NOT NULL,
    magic INTEGER NOT NULL,
    state TEXT NOT NULL,
    ticket INTEGER,
    mt5_retcode INTEGER,
    mt5_last_error TEXT,
    reason TEXT,
    operation_id TEXT,
    replaces_client_order_id TEXT,
    replaced_by_client_order_id TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS replace_operations (
    operation_id TEXT PRIMARY KEY,
    original_client_order_id TEXT NOT NULL,
    new_client_order_id TEXT NOT NULL,
    payload_hash TEXT NOT NULL,
    outcome TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS sequence_log (
    stream TEXT NOT NULL,
    seq INTEGER NOT NULL,
    payload_json TEXT NOT NULL,
    ts_utc TEXT NOT NULL,
    PRIMARY KEY (stream, seq)
);

CREATE TABLE IF NOT EXISTS sequence_log_floor (
    stream TEXT PRIMARY KEY,
    floor_seq INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS audit_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts_utc TEXT NOT NULL,
    account_id TEXT,
    method TEXT NOT NULL,
    path TEXT NOT NULL,
    payload_hash TEXT,
    outcome TEXT NOT NULL,
    detail TEXT
);

CREATE TABLE IF NOT EXISTS kill_switch_state (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    state TEXT NOT NULL,
    reason TEXT,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS dead_man_lease (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    expires_at_ms INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS poller_cursors (
    name TEXT PRIMARY KEY,
    cursor_epoch INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS daily_loss_baseline (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    account_id TEXT NOT NULL,
    trading_day TEXT NOT NULL,
    baseline_equity TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
"""


class BridgeDatabase:
    """Owns the one SQLite connection the bridge process keeps open."""

    def __init__(self, path: str) -> None:
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        with self._lock, self._conn:
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.executescript(_SCHEMA)
        self._restrict_file_permissions(path)

    @staticmethod
    def _restrict_file_permissions(path: str) -> None:
        """Plan §112: the journal/audit trail holds order and account detail,
        so its file (and WAL-mode's ``-wal``/``-shm`` sidecars) must not be
        group- or world-readable — 0600, owner-only, set right after the
        schema is created. Skipped for ``:memory:`` and other non-file
        SQLite URIs, which have no filesystem path to restrict."""
        if path in (":memory:", ""):
            return
        for candidate in (path, f"{path}-wal", f"{path}-shm"):
            if os.path.exists(candidate):
                os.chmod(candidate, _OWNER_READ_WRITE)

    @contextmanager
    def transaction(self) -> Generator[sqlite3.Connection]:
        """Run a block atomically: commits on success, rolls back on error."""
        with self._lock, self._conn:
            yield self._conn

    def fetchall(self, sql: str, params: tuple[object, ...] = ()) -> list[sqlite3.Row]:
        with self._lock:
            return list(self._conn.execute(sql, params))

    def fetchone(self, sql: str, params: tuple[object, ...] = ()) -> sqlite3.Row | None:
        with self._lock:
            return self._conn.execute(sql, params).fetchone()

    def close(self) -> None:
        with self._lock:
            self._conn.close()
