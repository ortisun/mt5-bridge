"""Boot reconciliation: a crash between send and journal write resolves to
either the real terminal state (unique match) or UNKNOWN (never a guess).
"""

from datetime import UTC, datetime, timedelta
from decimal import Decimal

from mt5_bridge.core.database import BridgeDatabase
from mt5_bridge.core.oms_state import BridgeOrderState
from mt5_bridge.core.order_journal import OrderJournal
from mt5_bridge.core.reconciliation import derive_tag, reconcile_on_boot
from mt5_bridge.core.sequence_log import SequenceLog
from mt5_bridge.mt5.fake_client import FakeMt5Client
from mt5_bridge.mt5.models import (
    FillingMode,
    Mt5Credentials,
    NewOrderIntent,
    OrderSide,
    OrderType,
    TimeInForce,
)

from testkit.clock import FixedClock
from testkit.fixtures import WINZ25, default_account, default_symbols

NOW_MS = 1_700_000_000_000
NOW = datetime.fromtimestamp(NOW_MS / 1000, tz=UTC)
WINDOW = timedelta(hours=1)
MAGIC = 777


def make_pieces() -> tuple[OrderJournal, SequenceLog, BridgeDatabase, FakeMt5Client, FixedClock]:
    db = BridgeDatabase(":memory:")
    journal = OrderJournal(db)
    sequence_log = SequenceLog(db)
    clock = FixedClock(NOW_MS)
    client = FakeMt5Client(symbols=default_symbols(), account=default_account(), clock=clock)
    client.connect(Mt5Credentials(login=1, password="x", server="Demo"))
    return journal, sequence_log, db, client, clock


def leave_stuck_in_received(
    journal: OrderJournal, db: BridgeDatabase, client_order_id: str
) -> None:
    """Simulate the crash: RECEIVED was committed, nothing else ever ran."""
    with db.transaction() as conn:
        journal.begin_receive(
            conn,
            client_order_id=client_order_id,
            payload_hash="hash",
            symbol=WINZ25,
            side=OrderSide.BUY,
            order_type=OrderType.LIMIT,
            volume=Decimal(1),
            price=Decimal(100),
            time_in_force=TimeInForce.DAY,
            magic=MAGIC,
            now=NOW,
        )


def send_directly_to_broker(client: FakeMt5Client, *, comment: str) -> int:
    """Simulate the order having actually reached the broker."""
    result = client.order_send(
        NewOrderIntent(
            symbol=WINZ25,
            side=OrderSide.BUY,
            order_type=OrderType.LIMIT,
            price=Decimal(100),
            volume=Decimal(1),
            time_in_force=TimeInForce.DAY,
            filling_mode=FillingMode.RETURN,
            magic=MAGIC,
            comment=comment,
        )
    )
    assert result.order is not None
    return result.order


class TestNoOp:
    def test_no_unconfirmed_entries_is_a_no_op(self) -> None:
        journal, sequence_log, db, client, clock = make_pieces()

        report = reconcile_on_boot(
            journal=journal,
            sequence_log=sequence_log,
            db=db,
            client=client,
            clock=clock,
            window=WINDOW,
        )

        assert report.resolved == ()
        assert report.unknown == ()


class TestUniqueMatchResolves:
    def test_a_uniquely_tagged_broker_order_resolves_the_crashed_entry(self) -> None:
        journal, sequence_log, db, client, clock = make_pieces()
        client_order_id = "co-crash-1"
        tag = derive_tag(client_order_id)
        leave_stuck_in_received(journal, db, client_order_id)
        send_directly_to_broker(client, comment=tag)

        report = reconcile_on_boot(
            journal=journal,
            sequence_log=sequence_log,
            db=db,
            client=client,
            clock=clock,
            window=WINDOW,
        )

        assert report.resolved == (client_order_id,)
        assert report.unknown == ()
        entry = journal.get_no_transaction(client_order_id)
        assert entry is not None
        assert entry.state is BridgeOrderState.ACKNOWLEDGED
        assert entry.ticket is not None

    def test_resolution_appends_an_executions_sequence_log_event(self) -> None:
        journal, sequence_log, db, client, clock = make_pieces()
        client_order_id = "co-crash-2"
        tag = derive_tag(client_order_id)
        leave_stuck_in_received(journal, db, client_order_id)
        send_directly_to_broker(client, comment=tag)

        reconcile_on_boot(
            journal=journal,
            sequence_log=sequence_log,
            db=db,
            client=client,
            clock=clock,
            window=WINDOW,
        )

        result = sequence_log.read_from_no_transaction(stream="executions", from_seq=1)
        assert len(result.events) == 1
        assert client_order_id in result.events[0].payload_json


class TestUnresolvedStaysUnknown:
    def test_no_matching_broker_order_leaves_it_unknown(self) -> None:
        journal, sequence_log, db, client, clock = make_pieces()
        client_order_id = "co-lost"
        leave_stuck_in_received(journal, db, client_order_id)
        # Nothing sent to the broker at all: the send never went out, or the
        # bridge crashed even before that.

        report = reconcile_on_boot(
            journal=journal,
            sequence_log=sequence_log,
            db=db,
            client=client,
            clock=clock,
            window=WINDOW,
        )

        assert report.resolved == ()
        assert report.unknown == (client_order_id,)
        entry = journal.get_no_transaction(client_order_id)
        assert entry is not None
        assert entry.state is BridgeOrderState.UNKNOWN

    def test_an_ambiguous_multi_candidate_match_leaves_it_unknown(self) -> None:
        journal, sequence_log, db, client, clock = make_pieces()
        client_order_id = "co-ambiguous"
        tag = derive_tag(client_order_id)
        leave_stuck_in_received(journal, db, client_order_id)
        send_directly_to_broker(client, comment=tag)
        send_directly_to_broker(client, comment=tag)  # two candidates, same tag+magic

        report = reconcile_on_boot(
            journal=journal,
            sequence_log=sequence_log,
            db=db,
            client=client,
            clock=clock,
            window=WINDOW,
        )

        assert report.unknown == (client_order_id,)
        entry = journal.get_no_transaction(client_order_id)
        assert entry is not None
        assert entry.state is BridgeOrderState.UNKNOWN
