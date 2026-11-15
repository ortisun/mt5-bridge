"""Pollers: closed-bars-only, tick dedup, book-change dedup, and execution
history reconciliation."""

from decimal import Decimal

from mt5_bridge.core.database import BridgeDatabase
from mt5_bridge.core.oms_state import BridgeOrderState
from mt5_bridge.core.order_journal import OrderJournal
from mt5_bridge.core.pollers import BarPoller, BookPoller, ExecutionPoller, TickPoller
from mt5_bridge.core.sequence_log import SequenceLog
from mt5_bridge.mt5.fake_client import FakeMt5Client
from mt5_bridge.mt5.models import (
    Bar,
    BookLevel,
    BookSide,
    FillingMode,
    Mt5Credentials,
    NewOrderIntent,
    OrderSide,
    OrderType,
    Timeframe,
    TimeInForce,
)

from testkit.clock import FixedClock
from testkit.fixtures import WINZ25, default_account, default_symbols

NOW_MS = 1_700_000_000_000


def connected_client(clock: FixedClock) -> FakeMt5Client:
    client = FakeMt5Client(symbols=default_symbols(), account=default_account(), clock=clock)
    client.connect(Mt5Credentials(login=1, password="x", server="Demo"))
    return client


class TestTickPoller:
    def test_the_first_tick_seen_is_an_event(self) -> None:
        clock = FixedClock(NOW_MS)
        client = connected_client(clock)
        client.set_tick(
            WINZ25, bid=Decimal(100), ask=Decimal(101), last=Decimal(100), volume=Decimal(1)
        )
        poller = TickPoller()

        events = poller.poll_once(client, [WINZ25])

        assert len(events) == 1

    def test_polling_again_without_a_new_tick_is_empty(self) -> None:
        clock = FixedClock(NOW_MS)
        client = connected_client(clock)
        client.set_tick(
            WINZ25, bid=Decimal(100), ask=Decimal(101), last=Decimal(100), volume=Decimal(1)
        )
        poller = TickPoller()
        poller.poll_once(client, [WINZ25])

        events = poller.poll_once(client, [WINZ25])

        assert events == ()

    def test_a_newer_tick_is_published(self) -> None:
        clock = FixedClock(NOW_MS)
        client = connected_client(clock)
        client.set_tick(
            WINZ25, bid=Decimal(100), ask=Decimal(101), last=Decimal(100), volume=Decimal(1)
        )
        poller = TickPoller()
        poller.poll_once(client, [WINZ25])
        clock.advance(1)
        client.set_tick(
            WINZ25, bid=Decimal(102), ask=Decimal(103), last=Decimal(102), volume=Decimal(1)
        )

        events = poller.poll_once(client, [WINZ25])

        assert len(events) == 1
        assert events[0].bid == Decimal(102)

    def test_a_symbol_with_no_tick_yet_yields_nothing(self) -> None:
        clock = FixedClock(NOW_MS)
        client = connected_client(clock)
        poller = TickPoller()

        events = poller.poll_once(client, [WINZ25])

        assert events == ()


class _ForceFormingBarClient:
    """A minimal, deliberately misbehaving Mt5Client double.

    Returns a forming bar even at start_pos=1, to prove BarPoller does not
    trust the raw source's own exclusion (defense in depth, plan rule 28).
    """

    def copy_bars(
        self, symbol: str, timeframe: Timeframe, start_pos: int, count: int
    ) -> tuple[Bar, ...]:
        del symbol, timeframe, start_pos, count
        return (
            Bar(
                symbol=WINZ25,
                timeframe=Timeframe.D1,
                time=1,
                open=Decimal(1),
                high=Decimal(1),
                low=Decimal(1),
                close=Decimal(1),
                volume=Decimal(1),
                is_closed=False,
            ),
        )


class TestBarPoller:
    def test_a_closed_bar_is_published(self) -> None:
        clock = FixedClock(NOW_MS)
        client = connected_client(clock)
        client.push_bar(WINZ25, Timeframe.D1, time=1, o=1, h=2, low=1, c=1, volume=10)
        client.push_bar(WINZ25, Timeframe.D1, time=2, o=1, h=2, low=1, c=2, volume=10)
        poller = BarPoller()

        events = poller.poll_once(client, [(WINZ25, Timeframe.D1)])

        assert len(events) == 1
        assert events[0].is_closed is True

    def test_polling_again_without_a_new_closed_bar_is_empty(self) -> None:
        clock = FixedClock(NOW_MS)
        client = connected_client(clock)
        client.push_bar(WINZ25, Timeframe.D1, time=1, o=1, h=2, low=1, c=1, volume=10)
        client.push_bar(WINZ25, Timeframe.D1, time=2, o=1, h=2, low=1, c=2, volume=10)
        poller = BarPoller()
        poller.poll_once(client, [(WINZ25, Timeframe.D1)])

        events = poller.poll_once(client, [(WINZ25, Timeframe.D1)])

        assert events == ()

    def test_two_bars_closing_between_polls_both_publish_once(self) -> None:
        clock = FixedClock(NOW_MS)
        client = connected_client(clock)
        client.push_bar(WINZ25, Timeframe.D1, time=1, o=1, h=2, low=1, c=1, volume=10)
        poller = BarPoller()
        client.push_bar(WINZ25, Timeframe.D1, time=2, o=1, h=2, low=1, c=2, volume=10)
        client.push_bar(WINZ25, Timeframe.D1, time=3, o=1, h=2, low=1, c=3, volume=10)

        events = poller.poll_once(client, [(WINZ25, Timeframe.D1)])

        assert [bar.time for bar in events] == [1, 2]
        assert poller.poll_once(client, [(WINZ25, Timeframe.D1)]) == ()

    def test_a_forming_bar_is_never_published_even_if_the_client_returns_one(self) -> None:
        poller = BarPoller()

        events = poller.poll_once(_ForceFormingBarClient(), [(WINZ25, Timeframe.D1)])  # type: ignore[arg-type]

        assert events == ()


class TestBookPoller:
    def test_the_first_snapshot_seen_is_an_event(self) -> None:
        clock = FixedClock(NOW_MS)
        client = connected_client(clock)
        client.set_book(
            WINZ25, (BookLevel(side=BookSide.BUY, price=Decimal(100), volume=Decimal(10)),)
        )
        poller = BookPoller()

        events = poller.poll_once(client, [WINZ25])

        assert [symbol for symbol, _levels in events] == [WINZ25]

    def test_polling_again_without_a_change_is_empty(self) -> None:
        clock = FixedClock(NOW_MS)
        client = connected_client(clock)
        client.set_book(
            WINZ25, (BookLevel(side=BookSide.BUY, price=Decimal(100), volume=Decimal(10)),)
        )
        poller = BookPoller()
        poller.poll_once(client, [WINZ25])

        events = poller.poll_once(client, [WINZ25])

        assert events == ()

    def test_a_changed_snapshot_is_published(self) -> None:
        clock = FixedClock(NOW_MS)
        client = connected_client(clock)
        client.set_book(
            WINZ25, (BookLevel(side=BookSide.BUY, price=Decimal(100), volume=Decimal(10)),)
        )
        poller = BookPoller()
        poller.poll_once(client, [WINZ25])
        client.set_book(
            WINZ25, (BookLevel(side=BookSide.BUY, price=Decimal(101), volume=Decimal(10)),)
        )

        events = poller.poll_once(client, [WINZ25])

        assert len(events) == 1
        _symbol, levels = events[0]
        assert levels[0].price == Decimal(101)

    def test_a_symbol_with_no_depth_yields_nothing(self) -> None:
        clock = FixedClock(NOW_MS)
        client = connected_client(clock)
        poller = BookPoller()

        events = poller.poll_once(client, [WINZ25])

        assert events == ()


class TestExecutionPoller:
    def _pieces(self) -> tuple[ExecutionPoller, OrderJournal, FakeMt5Client, FixedClock]:
        clock = FixedClock(NOW_MS)
        client = connected_client(clock)
        client.set_tick(
            WINZ25, bid=Decimal(100), ask=Decimal(101), last=Decimal(100), volume=Decimal(1)
        )
        db = BridgeDatabase(":memory:")
        journal = OrderJournal(db)
        poller = ExecutionPoller(db=db, journal=journal, sequence_log=SequenceLog(db))
        return poller, journal, client, clock

    def _open_limit_order(
        self, journal: OrderJournal, client: FakeMt5Client, clock: FixedClock, client_order_id: str
    ) -> int:
        sent = client.order_send(
            NewOrderIntent(
                symbol=WINZ25,
                side=OrderSide.BUY,
                order_type=OrderType.LIMIT,
                price=Decimal(100),
                volume=Decimal(1),
                time_in_force=TimeInForce.DAY,
                filling_mode=FillingMode.RETURN,
                magic=1,
                comment=client_order_id,
            )
        )
        assert sent.order is not None
        with journal.db.transaction() as conn:
            journal.begin_receive(
                conn,
                client_order_id=client_order_id,
                payload_hash="h",
                symbol=WINZ25,
                side=OrderSide.BUY,
                order_type=OrderType.LIMIT,
                volume=Decimal(1),
                price=Decimal(100),
                time_in_force=TimeInForce.DAY,
                magic=1,
                now=clock.now_utc(),
            )
            journal.record_result(
                conn,
                client_order_id=client_order_id,
                state=BridgeOrderState.ACKNOWLEDGED,
                ticket=sent.order,
                mt5_retcode=10008,
                mt5_last_error=None,
                reason=None,
                now=clock.now_utc(),
            )
        return sent.order

    def test_no_open_entries_is_a_no_op(self) -> None:
        poller, _journal, client, clock = self._pieces()

        affected = poller.poll_once(client, clock)

        assert affected == ()

    def test_a_fill_detected_via_history_is_finalized(self) -> None:
        poller, journal, client, clock = self._pieces()
        ticket = self._open_limit_order(journal, client, clock, "co-1")
        client.force_fill(ticket)

        affected = poller.poll_once(client, clock)

        assert affected == ("co-1",)
        entry = journal.get_no_transaction("co-1")
        assert entry is not None
        assert entry.state is BridgeOrderState.FILLED

    def test_an_unchanged_open_order_is_not_republished(self) -> None:
        poller, journal, client, clock = self._pieces()
        self._open_limit_order(journal, client, clock, "co-2")

        affected = poller.poll_once(client, clock)

        assert affected == ()

    def test_the_cursor_persists_and_overlap_still_catches_edge_fills(self) -> None:
        poller, journal, client, clock = self._pieces()
        ticket = self._open_limit_order(journal, client, clock, "co-3")
        poller.poll_once(client, clock)  # advances the cursor to "now" with nothing to report
        cursor_after_first_poll = poller.cursor()

        client.force_fill(ticket)
        reopened = ExecutionPoller(db=poller.db, journal=journal, sequence_log=poller.sequence_log)

        affected = reopened.poll_once(client, clock)

        assert affected == ("co-3",)
        assert reopened.cursor() >= cursor_after_first_poll
