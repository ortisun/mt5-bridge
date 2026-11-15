"""Append-only audit log of every write request (plan §112, §114).

Rows are only ever inserted, never updated or deleted, through the same
``BridgeDatabase`` transaction primitive as everything else. Route handlers
call ``record`` explicitly after a write completes (success or failure) so
the recorded ``outcome`` reflects what actually happened, not just that a
request arrived.
"""

import sqlite3
from dataclasses import dataclass
from datetime import datetime

from mt5_bridge.core.database import BridgeDatabase


@dataclass(frozen=True, slots=True)
class AuditRecord:
    ts_utc: datetime
    account_id: str | None
    method: str
    path: str
    payload_hash: str | None
    outcome: str
    detail: str | None


def _row_to_record(row: sqlite3.Row) -> AuditRecord:
    return AuditRecord(
        ts_utc=datetime.fromisoformat(row["ts_utc"]),
        account_id=row["account_id"],
        method=row["method"],
        path=row["path"],
        payload_hash=row["payload_hash"],
        outcome=row["outcome"],
        detail=row["detail"],
    )


@dataclass(frozen=True, slots=True)
class AuditLog:
    db: BridgeDatabase

    def record(
        self,
        *,
        now: datetime,
        account_id: str | None,
        method: str,
        path: str,
        payload_hash: str | None,
        outcome: str,
        detail: str | None = None,
    ) -> None:
        with self.db.transaction() as conn:
            conn.execute(
                "INSERT INTO audit_log "
                "(ts_utc, account_id, method, path, payload_hash, outcome, detail) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (now.isoformat(), account_id, method, path, payload_hash, outcome, detail),
            )

    def list_no_transaction(self) -> tuple[AuditRecord, ...]:
        rows = self.db.fetchall("SELECT * FROM audit_log ORDER BY id")
        return tuple(_row_to_record(row) for row in rows)
