"""Durable, replayable, per-stream sequence numbers (plan §114 WebSocket v1).

Only ``executions`` is wired through this in practice (market channels get a
per-connection, non-durable sequence in ``api/ws.py`` — the plan explicitly
does not require ticks/bars/book to be replayable). ``append`` takes a
caller-supplied connection so it can be committed atomically alongside an
``OrderJournal`` write; resuming past the retained window returns
``RESYNC_REQUIRED`` rather than silently skipping events.
"""

import sqlite3
from dataclasses import dataclass
from datetime import datetime
from enum import Enum, auto

from mt5_bridge.core.database import BridgeDatabase


class ResumeStatus(Enum):
    OK = auto()
    RESYNC_REQUIRED = auto()


@dataclass(frozen=True, slots=True)
class SequenceEvent:
    seq: int
    payload_json: str
    ts_utc: datetime


@dataclass(frozen=True, slots=True)
class ResumeResult:
    status: ResumeStatus
    events: tuple[SequenceEvent, ...]


class SequenceLog:
    def __init__(self, db: BridgeDatabase) -> None:
        self._db = db

    def append(
        self, conn: sqlite3.Connection, *, stream: str, payload_json: str, ts_utc: datetime
    ) -> int:
        row = conn.execute(
            "SELECT COALESCE(MAX(seq), 0) + 1 AS next_seq FROM sequence_log WHERE stream = ?",
            (stream,),
        ).fetchone()
        next_seq: int = row["next_seq"]
        conn.execute(
            "INSERT INTO sequence_log (stream, seq, payload_json, ts_utc) VALUES (?, ?, ?, ?)",
            (stream, next_seq, payload_json, ts_utc.isoformat()),
        )
        return next_seq

    def oldest_seq(self, conn: sqlite3.Connection, *, stream: str) -> int | None:
        row = conn.execute(
            "SELECT MIN(seq) AS oldest FROM sequence_log WHERE stream = ?", (stream,)
        ).fetchone()
        oldest: int | None = row["oldest"]
        return oldest

    def _floor(self, conn: sqlite3.Connection, *, stream: str) -> int:
        """The highest seq ever pruned away for this stream (0 if none).

        Tracked separately from the currently-present rows: a stream that
        has been pruned all the way to empty must still refuse a stale
        ``from_seq`` rather than reporting "nothing to send" (which would
        look identical to "you are caught up" and silently drop history).
        """
        row = conn.execute(
            "SELECT floor_seq FROM sequence_log_floor WHERE stream = ?", (stream,)
        ).fetchone()
        return row["floor_seq"] if row is not None else 0

    def read_from(self, conn: sqlite3.Connection, *, stream: str, from_seq: int) -> ResumeResult:
        if from_seq < 1:
            raise ValueError("from_seq must be >= 1")
        if from_seq <= self._floor(conn, stream=stream):
            return ResumeResult(status=ResumeStatus.RESYNC_REQUIRED, events=())
        rows = conn.execute(
            "SELECT seq, payload_json, ts_utc FROM sequence_log "
            "WHERE stream = ? AND seq >= ? ORDER BY seq",
            (stream, from_seq),
        ).fetchall()
        events = tuple(
            SequenceEvent(
                seq=row["seq"],
                payload_json=row["payload_json"],
                ts_utc=datetime.fromisoformat(row["ts_utc"]),
            )
            for row in rows
        )
        return ResumeResult(status=ResumeStatus.OK, events=events)

    def read_from_no_transaction(self, *, stream: str, from_seq: int) -> ResumeResult:
        with self._db.transaction() as conn:
            return self.read_from(conn, stream=stream, from_seq=from_seq)

    def prune_no_transaction(self, *, stream: str, keep_last: int) -> None:
        with self._db.transaction() as conn:
            self.prune(conn, stream=stream, keep_last=keep_last)

    def prune(self, conn: sqlite3.Connection, *, stream: str, keep_last: int) -> None:
        """Drop everything older than the most recent ``keep_last`` events."""
        row = conn.execute(
            "SELECT MAX(seq) AS max_seq FROM sequence_log WHERE stream = ?", (stream,)
        ).fetchone()
        max_seq: int | None = row["max_seq"]
        if max_seq is None:
            return
        new_floor = max_seq - keep_last
        if new_floor <= self._floor(conn, stream=stream):
            return
        conn.execute("DELETE FROM sequence_log WHERE stream = ? AND seq <= ?", (stream, new_floor))
        conn.execute(
            "INSERT INTO sequence_log_floor (stream, floor_seq) VALUES (?, ?) "
            "ON CONFLICT(stream) DO UPDATE SET floor_seq = excluded.floor_seq",
            (stream, new_floor),
        )
