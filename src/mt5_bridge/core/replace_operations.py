"""The durable, idempotent record of every cancel-replace operation.

Mirrors ``OrderJournal``'s idempotency shape but for ``operation_id`` (plan
§114: "The response is the discriminated outcome from §56, queryable by
operation_id. Repetition returns the operation state; a different payload
with the same ID is a conflict.").
"""

import sqlite3
from dataclasses import dataclass
from datetime import datetime
from enum import Enum, auto

from mt5_bridge.core.database import BridgeDatabase
from mt5_bridge.core.oms_state import ReplaceOutcome


class ReceiveOutcome(Enum):
    NEW = auto()
    DUPLICATE_SAME_PAYLOAD = auto()
    CONFLICT = auto()


@dataclass(frozen=True, slots=True)
class ReplaceOperationRow:
    operation_id: str
    original_client_order_id: str
    new_client_order_id: str
    payload_hash: str
    outcome: ReplaceOutcome
    created_at: datetime
    updated_at: datetime


def _row_to_operation(row: sqlite3.Row) -> ReplaceOperationRow:
    return ReplaceOperationRow(
        operation_id=row["operation_id"],
        original_client_order_id=row["original_client_order_id"],
        new_client_order_id=row["new_client_order_id"],
        payload_hash=row["payload_hash"],
        outcome=ReplaceOutcome(row["outcome"]),
        created_at=datetime.fromisoformat(row["created_at"]),
        updated_at=datetime.fromisoformat(row["updated_at"]),
    )


class ReplaceOperationStore:
    def __init__(self, db: BridgeDatabase) -> None:
        self.db = db

    def begin_receive(
        self,
        conn: sqlite3.Connection,
        *,
        operation_id: str,
        original_client_order_id: str,
        new_client_order_id: str,
        payload_hash: str,
        now: datetime,
    ) -> tuple[ReceiveOutcome, ReplaceOperationRow]:
        existing = self.get(conn, operation_id)
        if existing is not None:
            outcome = (
                ReceiveOutcome.DUPLICATE_SAME_PAYLOAD
                if existing.payload_hash == payload_hash
                else ReceiveOutcome.CONFLICT
            )
            return outcome, existing

        conn.execute(
            """
            INSERT INTO replace_operations (
                operation_id, original_client_order_id, new_client_order_id,
                payload_hash, outcome, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                operation_id,
                original_client_order_id,
                new_client_order_id,
                payload_hash,
                ReplaceOutcome.PENDING.value,
                now.isoformat(),
                now.isoformat(),
            ),
        )
        entry = self.get(conn, operation_id)
        if entry is None:
            raise RuntimeError("replace_operations row vanished immediately after insert")
        return ReceiveOutcome.NEW, entry

    def update_outcome(
        self, conn: sqlite3.Connection, *, operation_id: str, outcome: ReplaceOutcome, now: datetime
    ) -> None:
        conn.execute(
            "UPDATE replace_operations SET outcome = ?, updated_at = ? WHERE operation_id = ?",
            (outcome.value, now.isoformat(), operation_id),
        )

    def get(self, conn: sqlite3.Connection, operation_id: str) -> ReplaceOperationRow | None:
        row = conn.execute(
            "SELECT * FROM replace_operations WHERE operation_id = ?", (operation_id,)
        ).fetchone()
        return _row_to_operation(row) if row is not None else None

    def get_no_transaction(self, operation_id: str) -> ReplaceOperationRow | None:
        row = self.db.fetchone(
            "SELECT * FROM replace_operations WHERE operation_id = ?", (operation_id,)
        )
        return _row_to_operation(row) if row is not None else None
