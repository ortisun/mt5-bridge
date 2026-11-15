"""Boot reconciliation (plan §114 "Order Idempotency", §56 UNKNOWN).

A crash between ``order_send`` and the journal write leaves an entry stuck in
``RECEIVED``. At boot, this module tries to match it against the broker's
own view (``orders_get``/``history_orders_get``) using the magic number and
the short comment tag the bridge derived from ``client_order_id`` at send
time. A unique match resolves the entry to the terminal's real state; zero or
several candidates leave it ``UNKNOWN`` — reconciliation never guesses.
"""

import hashlib
from collections import defaultdict
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta

from opentelemetry import trace

from mt5_bridge.clock import Clock
from mt5_bridge.core.database import BridgeDatabase
from mt5_bridge.core.execution_events import finalize_and_publish
from mt5_bridge.core.oms_state import BridgeOrderState, map_terminal_state
from mt5_bridge.core.order_journal import JournalEntry, OrderJournal
from mt5_bridge.core.sequence_log import SequenceLog
from mt5_bridge.core.tracing import traced_sync
from mt5_bridge.mt5.client import Mt5Client
from mt5_bridge.mt5.models import Mt5Deal, Mt5Order

_TAG_MAX_LEN = 24


def derive_tag(client_order_id: str, *, max_len: int = _TAG_MAX_LEN) -> str:
    """A short, deterministic recovery hint for MT5's ``comment`` field.

    Never a unique key on its own (the broker may alter ``comment`` — plan
    §114): reconciliation additionally requires the candidate's ``magic`` to
    match, and still falls back to ``UNKNOWN`` on anything but a single
    unambiguous match.
    """
    if len(client_order_id) <= max_len:
        return client_order_id
    digest = hashlib.sha256(client_order_id.encode()).hexdigest()
    return f"h:{digest[: max_len - 2]}"


@dataclass(frozen=True, slots=True)
class ReconciliationReport:
    resolved: tuple[str, ...]
    unknown: tuple[str, ...]


def _index_by_tag(orders: list[Mt5Order]) -> dict[str, list[Mt5Order]]:
    index: dict[str, list[Mt5Order]] = defaultdict(list)
    for order in orders:
        index[order.comment].append(order)
    return index


@traced_sync("mt5_bridge.reconciliation.boot")
def reconcile_on_boot(
    *,
    journal: OrderJournal,
    sequence_log: SequenceLog,
    db: BridgeDatabase,
    client: Mt5Client,
    clock: Clock,
    window: timedelta,
) -> ReconciliationReport:
    pending = journal.list_unconfirmed_no_transaction()
    boot_span = trace.get_current_span()
    boot_span.set_attribute("mt5_bridge.reconciliation.pending", len(pending))
    if not pending:
        return ReconciliationReport(resolved=(), unknown=())

    now = clock.now_utc()
    window_start = int((now - window).timestamp())
    now_epoch = int(now.timestamp())
    broker_orders = [*client.orders(), *client.history_orders(window_start, now_epoch)]
    by_tag = _index_by_tag(broker_orders)

    resolved: list[str] = []
    unknown: list[str] = []
    for entry in pending:
        tag = derive_tag(entry.client_order_id)
        candidates = {
            order.ticket: order for order in by_tag.get(tag, ()) if order.magic == entry.magic
        }
        if len(candidates) == 1:
            [order] = candidates.values()
            state = map_terminal_state(order.state)
            ticket: int | None = order.ticket
            reason = "resolved at boot reconciliation"
        else:
            state = BridgeOrderState.UNKNOWN
            ticket = None
            reason = (
                "no matching broker order within the reconciliation window"
                if not candidates
                else "ambiguous: multiple broker orders share this tag and magic"
            )
        finalize_and_publish(
            db=db,
            journal=journal,
            sequence_log=sequence_log,
            client_order_id=entry.client_order_id,
            state=state,
            ticket=ticket,
            mt5_retcode=None,
            mt5_last_error=None,
            reason=reason,
            now=now,
        )
        if state is BridgeOrderState.UNKNOWN:
            unknown.append(entry.client_order_id)
        else:
            resolved.append(entry.client_order_id)

    boot_span.set_attribute("mt5_bridge.reconciliation.resolved", len(resolved))
    boot_span.set_attribute("mt5_bridge.reconciliation.unknown", len(unknown))
    return ReconciliationReport(resolved=tuple(resolved), unknown=tuple(unknown))


def classify_unconfirmed(
    entry: JournalEntry,
    *,
    broker_orders: Sequence[Mt5Order],
    deals: Sequence[Mt5Deal],
    window_start: datetime,
) -> tuple[BridgeOrderState, int | None, str]:
    """Decide an UNKNOWN/RECEIVED entry from the terminal's own view; never guess.

    A known ticket takes the terminal's state for that ticket. Without a
    ticket, a unique magic+tag candidate resolves it as at boot. It becomes
    REJECTED ("never sent") only when the window covers the entry and the
    terminal holds no order and no deal with this bridge's magic at all; any
    other case stays UNKNOWN for the operator.
    """
    if entry.ticket is not None:
        match = next((order for order in broker_orders if order.ticket == entry.ticket), None)
        if match is None:
            return BridgeOrderState.UNKNOWN, entry.ticket, "ticket not found at the terminal"
        return map_terminal_state(match.state), entry.ticket, "reconciled against the terminal"
    ours = [order for order in broker_orders if order.magic == entry.magic]
    tag = derive_tag(entry.client_order_id)
    candidates = {order.ticket: order for order in ours if order.comment == tag}
    if len(candidates) == 1:
        [order] = candidates.values()
        return map_terminal_state(order.state), order.ticket, "reconciled against the terminal"
    if (
        not ours
        and not any(deal.magic == entry.magic for deal in deals)
        and entry.created_at >= window_start
    ):
        return (
            BridgeOrderState.REJECTED,
            None,
            "no order or deal with this bridge's magic at the terminal: never sent",
        )
    return (
        BridgeOrderState.UNKNOWN,
        None,
        "not provable from the terminal: other bridge orders or deals share the window",
    )
