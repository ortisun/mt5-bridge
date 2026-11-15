"""The durable record of every order the bridge has ever received.

Implements the idempotency contract in plan §114/§86: a ``client_order_id``
is inserted exactly once, in ``RECEIVED`` state, before any terminal call is
attempted. Replaying the same payload returns the existing entry; a
different payload under the same id is a conflict. ``record_result`` and
``mark_unknown`` are the only ways an entry leaves ``RECEIVED`` — always
inside a transaction the caller controls, so it can be paired atomically
with a ``SequenceLog`` append (the "share a local transaction" requirement).
"""

import sqlite3
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from enum import Enum, auto

from mt5_bridge.core.database import BridgeDatabase
from mt5_bridge.core.oms_state import TERMINAL_STATES, BridgeOrderState
from mt5_bridge.mt5.models import OrderSide, OrderType, TimeInForce


class ReceiveOutcome(Enum):
    NEW = auto()
    DUPLICATE_SAME_PAYLOAD = auto()
    CONFLICT = auto()


@dataclass(frozen=True, slots=True)
class JournalEntry:
    client_order_id: str
    payload_hash: str
    symbol: str
    side: OrderSide
    order_type: OrderType
    volume: Decimal
    price: Decimal | None
    notional_price: Decimal | None
    """A conservative fresh-quote bound recorded at submit for an order whose
    own ``price`` is unknown (a MARKET order) — the only way LimitsGuard's
    open-notional exposure can be recovered after a restart for an order that
    never had a price to begin with (audit F2)."""
    time_in_force: TimeInForce
    magic: int
    state: BridgeOrderState
    ticket: int | None
    mt5_retcode: int | None
    mt5_last_error: str | None
    reason: str | None
    operation_id: str | None
    replaces_client_order_id: str | None
    replaced_by_client_order_id: str | None
    created_at: datetime
    updated_at: datetime


def _row_to_entry(row: sqlite3.Row) -> JournalEntry:
    return JournalEntry(
        client_order_id=row["client_order_id"],
        payload_hash=row["payload_hash"],
        symbol=row["symbol"],
        side=OrderSide(row["side"]),
        order_type=OrderType(row["order_type"]),
        volume=Decimal(row["volume"]),
        price=Decimal(row["price"]) if row["price"] is not None else None,
        notional_price=Decimal(row["notional_price"])
        if row["notional_price"] is not None
        else None,
        time_in_force=TimeInForce(row["time_in_force"]),
        magic=row["magic"],
        state=BridgeOrderState(row["state"]),
        ticket=row["ticket"],
        mt5_retcode=row["mt5_retcode"],
        mt5_last_error=row["mt5_last_error"],
        reason=row["reason"],
        operation_id=row["operation_id"],
        replaces_client_order_id=row["replaces_client_order_id"],
        replaced_by_client_order_id=row["replaced_by_client_order_id"],
        created_at=datetime.fromisoformat(row["created_at"]),
        updated_at=datetime.fromisoformat(row["updated_at"]),
    )


class OrderJournal:
    def __init__(self, db: BridgeDatabase) -> None:
        self.db = db

    def begin_receive(
        self,
        conn: sqlite3.Connection,
        *,
        client_order_id: str,
        payload_hash: str,
        symbol: str,
        side: OrderSide,
        order_type: OrderType,
        volume: Decimal,
        price: Decimal | None,
        time_in_force: TimeInForce,
        magic: int,
        now: datetime,
        operation_id: str | None = None,
        replaces_client_order_id: str | None = None,
    ) -> tuple[ReceiveOutcome, JournalEntry]:
        existing = self.get(conn, client_order_id)
        if existing is not None:
            outcome = (
                ReceiveOutcome.DUPLICATE_SAME_PAYLOAD
                if existing.payload_hash == payload_hash
                else ReceiveOutcome.CONFLICT
            )
            return outcome, existing

        conn.execute(
            """
            INSERT INTO order_journal (
                client_order_id, payload_hash, symbol, side, order_type, volume,
                price, notional_price, time_in_force, magic, state, ticket, mt5_retcode,
                mt5_last_error, reason, operation_id, replaces_client_order_id,
                replaced_by_client_order_id, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, NULL, ?, ?, ?, NULL, NULL, NULL, NULL, ?, ?, NULL, ?, ?)
            """,
            (
                client_order_id,
                payload_hash,
                symbol,
                side.value,
                order_type.value,
                str(volume),
                str(price) if price is not None else None,
                time_in_force.value,
                magic,
                BridgeOrderState.RECEIVED.value,
                operation_id,
                replaces_client_order_id,
                now.isoformat(),
                now.isoformat(),
            ),
        )
        entry = self.get(conn, client_order_id)
        if entry is None:
            raise RuntimeError("order_journal row vanished immediately after insert")
        return ReceiveOutcome.NEW, entry

    def record_result(
        self,
        conn: sqlite3.Connection,
        *,
        client_order_id: str,
        state: BridgeOrderState,
        ticket: int | None,
        mt5_retcode: int | None,
        mt5_last_error: str | None,
        reason: str | None,
        now: datetime,
    ) -> None:
        conn.execute(
            """
            UPDATE order_journal
            SET state = ?, ticket = ?, mt5_retcode = ?, mt5_last_error = ?,
                reason = ?, updated_at = ?
            WHERE client_order_id = ?
            """,
            (
                state.value,
                ticket,
                mt5_retcode,
                mt5_last_error,
                reason,
                now.isoformat(),
                client_order_id,
            ),
        )

    def set_notional_price(
        self,
        conn: sqlite3.Connection,
        *,
        client_order_id: str,
        notional_price: Decimal,
        now: datetime,
    ) -> None:
        """Persist the fresh-quote bound computed for an unpriced (MARKET)
        order at submit time, so a restart can still recover its exposure
        (audit F2) — a follow-up update on the row ``begin_receive`` already
        inserted, mirroring how ``record_result`` updates the same row later
        in its lifecycle."""
        conn.execute(
            "UPDATE order_journal SET notional_price = ?, updated_at = ? WHERE client_order_id = ?",
            (str(notional_price), now.isoformat(), client_order_id),
        )

    def mark_unknown(
        self, conn: sqlite3.Connection, *, client_order_id: str, reason: str, now: datetime
    ) -> None:
        existing = self.get(conn, client_order_id)
        self.record_result(
            conn,
            client_order_id=client_order_id,
            state=BridgeOrderState.UNKNOWN,
            ticket=existing.ticket if existing else None,
            mt5_retcode=existing.mt5_retcode if existing else None,
            mt5_last_error=existing.mt5_last_error if existing else None,
            reason=reason,
            now=now,
        )

    def link_replacement(
        self,
        conn: sqlite3.Connection,
        *,
        original_client_order_id: str,
        new_client_order_id: str,
        now: datetime,
    ) -> None:
        conn.execute(
            "UPDATE order_journal SET replaced_by_client_order_id = ?, updated_at = ? "
            "WHERE client_order_id = ?",
            (new_client_order_id, now.isoformat(), original_client_order_id),
        )

    def get(self, conn: sqlite3.Connection, client_order_id: str) -> JournalEntry | None:
        row = conn.execute(
            "SELECT * FROM order_journal WHERE client_order_id = ?", (client_order_id,)
        ).fetchone()
        return _row_to_entry(row) if row is not None else None

    def get_no_transaction(self, client_order_id: str) -> JournalEntry | None:
        row = self.db.fetchone(
            "SELECT * FROM order_journal WHERE client_order_id = ?", (client_order_id,)
        )
        return _row_to_entry(row) if row is not None else None

    def list_unconfirmed_no_transaction(self) -> tuple[JournalEntry, ...]:
        rows = self.db.fetchall(
            "SELECT * FROM order_journal WHERE state IN (?, ?)",
            (BridgeOrderState.RECEIVED.value, BridgeOrderState.SUBMITTED.value),
        )
        return tuple(_row_to_entry(row) for row in rows)

    def get_by_ticket_no_transaction(self, ticket: int) -> JournalEntry | None:
        row = self.db.fetchone("SELECT * FROM order_journal WHERE ticket = ?", (ticket,))
        return _row_to_entry(row) if row is not None else None

    def count_created_since_no_transaction(self, *, since: datetime) -> int:
        row = self.db.fetchone(
            "SELECT COUNT(*) AS n FROM order_journal WHERE created_at >= ?", (since.isoformat(),)
        )
        return int(row["n"]) if row is not None else 0

    def has_unknown_for_symbol_no_transaction(self, symbol: str) -> bool:
        """Plan §56: an UNKNOWN order blocks new orders on the same instrument
        until reconciled — checked by ``OrderService.submit_order``."""
        row = self.db.fetchone(
            "SELECT 1 FROM order_journal WHERE symbol = ? AND state = ? LIMIT 1",
            (symbol, BridgeOrderState.UNKNOWN.value),
        )
        return row is not None

    def list_open_no_transaction(self) -> tuple[JournalEntry, ...]:
        """Entries a terminal fact has not yet closed out (used by pollers).

        Filters the terminal-state exclusion in Python rather than building a
        ``NOT IN (...)`` clause with a query-time placeholder count: the
        journal is VM-local and small, and this keeps every SQL string in
        this module a fixed literal.
        """
        rows = self.db.fetchall("SELECT * FROM order_journal WHERE ticket IS NOT NULL")
        return tuple(
            entry
            for entry in (_row_to_entry(row) for row in rows)
            if entry.state not in TERMINAL_STATES
        )

    def list_open_exposure_no_transaction(self) -> tuple[JournalEntry, ...]:
        """Every entry LimitsGuard's open-notional sum must still count as
        exposure — not just tickets already on the books
        (``list_open_no_transaction``) but anything still in flight: RECEIVED
        before a ticket exists, and UNKNOWN when a lost response's true
        outcome was never learned (plan rule 43: pending and UNKNOWN orders
        continue to consume capacity — audit F2's "restart retains
        pending/UNKNOWN exposure"). Unlike ``list_open_no_transaction``, this
        never filters on ``ticket``.
        """
        rows = self.db.fetchall("SELECT * FROM order_journal")
        return tuple(
            entry
            for entry in (_row_to_entry(row) for row in rows)
            if entry.state not in TERMINAL_STATES
        )
