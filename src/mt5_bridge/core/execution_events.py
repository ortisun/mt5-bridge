"""Atomically record a journal outcome and publish it.

The single shared implementation of plan §114's "OrderJournal and
SequenceLog share a local transaction; a crash before WS publication does
not lose the event." ``OrderService``, boot reconciliation, and
``ExecutionPoller`` all finalize an order through this one function so the
durability guarantee has exactly one implementation.
"""

import json
from datetime import datetime

from mt5_bridge.core.database import BridgeDatabase
from mt5_bridge.core.oms_state import BridgeOrderState
from mt5_bridge.core.order_journal import JournalEntry, OrderJournal
from mt5_bridge.core.sequence_log import SequenceLog


def build_execution_payload(entry: JournalEntry, *, reason: str | None) -> str:
    return json.dumps(
        {
            "client_order_id": entry.client_order_id,
            "symbol": entry.symbol,
            "state": entry.state.value,
            "ticket": entry.ticket,
            "mt5_retcode": entry.mt5_retcode,
            "reason": reason,
        },
        sort_keys=True,
    )


def finalize_and_publish(
    *,
    db: BridgeDatabase,
    journal: OrderJournal,
    sequence_log: SequenceLog,
    client_order_id: str,
    state: BridgeOrderState,
    ticket: int | None,
    mt5_retcode: int | None,
    mt5_last_error: str | None,
    reason: str | None,
    now: datetime,
) -> JournalEntry:
    with db.transaction() as conn:
        journal.record_result(
            conn,
            client_order_id=client_order_id,
            state=state,
            ticket=ticket,
            mt5_retcode=mt5_retcode,
            mt5_last_error=mt5_last_error,
            reason=reason,
            now=now,
        )
        entry = journal.get(conn, client_order_id)
        if entry is None:
            raise RuntimeError(f"journal entry for {client_order_id!r} vanished")
        sequence_log.append(
            conn,
            stream="executions",
            payload_json=build_execution_payload(entry, reason=reason),
            ts_utc=now,
        )
    return entry
