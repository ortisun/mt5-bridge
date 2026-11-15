"""Real LimitsState aggregates from the journal and a tracked account equity."""

from datetime import UTC, datetime
from decimal import Decimal

from mt5_bridge.core.database import BridgeDatabase
from mt5_bridge.core.limits_state_provider import AccountEquityTracker, compute_limits_state
from mt5_bridge.core.oms_state import BridgeOrderState
from mt5_bridge.core.order_journal import OrderJournal
from mt5_bridge.mt5.models import OrderSide, OrderType, TimeInForce

from testkit.clock import FixedClock

NOW_MS = 1_700_000_000_000
NOW = datetime.fromtimestamp(NOW_MS / 1000, tz=UTC)


def submit(
    journal: OrderJournal,
    client_order_id: str,
    *,
    symbol: str = "WINZ25",
    volume: Decimal = Decimal(1),
    price: Decimal | None = Decimal(100),
    now: datetime = NOW,
) -> None:
    with journal.db.transaction() as conn:
        journal.begin_receive(
            conn,
            client_order_id=client_order_id,
            payload_hash="h",
            symbol=symbol,
            side=OrderSide.BUY,
            order_type=OrderType.LIMIT,
            volume=volume,
            price=price,
            time_in_force=TimeInForce.DAY,
            magic=1,
            now=now,
        )


def acknowledge(journal: OrderJournal, client_order_id: str, *, ticket: int, now: datetime) -> None:
    with journal.db.transaction() as conn:
        journal.record_result(
            conn,
            client_order_id=client_order_id,
            state=BridgeOrderState.ACKNOWLEDGED,
            ticket=ticket,
            mt5_retcode=10008,
            mt5_last_error=None,
            reason=None,
            now=now,
        )


def mark_unknown(journal: OrderJournal, client_order_id: str, *, now: datetime) -> None:
    with journal.db.transaction() as conn:
        journal.mark_unknown(conn, client_order_id=client_order_id, reason="lost response", now=now)


class TestAccountEquityTracker:
    def test_no_recordings_yet_reports_zero_daily_loss(self) -> None:
        tracker = AccountEquityTracker()

        assert tracker.daily_loss() == Decimal(0)

    def test_the_first_record_establishes_the_session_baseline(self) -> None:
        tracker = AccountEquityTracker()

        tracker.record(Decimal(100_000))

        assert tracker.daily_loss() == Decimal(0)

    def test_a_later_drop_is_reported_as_a_positive_loss(self) -> None:
        tracker = AccountEquityTracker()
        tracker.record(Decimal(100_000))

        tracker.record(Decimal(97_500))

        assert tracker.daily_loss() == Decimal(2_500)

    def test_a_gain_never_produces_a_negative_loss(self) -> None:
        tracker = AccountEquityTracker()
        tracker.record(Decimal(100_000))

        tracker.record(Decimal(105_000))

        assert tracker.daily_loss() == Decimal(0)

    def test_the_baseline_does_not_move_after_it_is_established(self) -> None:
        tracker = AccountEquityTracker()
        tracker.record(Decimal(100_000))
        tracker.record(Decimal(105_000))  # a rally does not raise the bar

        tracker.record(Decimal(99_000))

        assert tracker.daily_loss() == Decimal(1_000)

    def test_record_without_now_never_sets_last_recorded_at(self) -> None:
        tracker = AccountEquityTracker()

        tracker.record(Decimal(100_000))

        assert tracker.last_recorded_at is None

    def test_record_with_now_sets_last_recorded_at(self) -> None:
        tracker = AccountEquityTracker()

        tracker.record(Decimal(100_000), now=NOW)

        assert tracker.last_recorded_at == NOW

    def test_mark_unavailable_records_the_reason(self) -> None:
        tracker = AccountEquityTracker()

        tracker.mark_unavailable("account mismatch")

        assert tracker.evidence_unavailable_reason == "account mismatch"


class TestComputeLimitsState:
    def test_an_empty_journal_yields_all_zeros(self) -> None:
        db = BridgeDatabase(":memory:")
        journal = OrderJournal(db)
        clock = FixedClock(NOW_MS)

        state = compute_limits_state(
            journal=journal, equity_tracker=AccountEquityTracker(), clock=clock
        )

        assert state.orders_in_last_minute == 0
        assert state.open_notional == Decimal(0)
        assert state.daily_loss == Decimal(0)

    def test_counts_orders_received_within_the_trailing_window(self) -> None:
        db = BridgeDatabase(":memory:")
        journal = OrderJournal(db)
        clock = FixedClock(NOW_MS)
        submit(journal, "co-old", now=NOW.replace(minute=0, second=0))
        submit(journal, "co-recent", now=NOW)

        state = compute_limits_state(
            journal=journal, equity_tracker=AccountEquityTracker(), clock=clock, window_seconds=60
        )

        assert state.orders_in_last_minute == 1

    def test_open_notional_sums_price_times_volume_for_open_priced_orders(self) -> None:
        db = BridgeDatabase(":memory:")
        journal = OrderJournal(db)
        clock = FixedClock(NOW_MS)
        submit(journal, "co-1", volume=Decimal(2), price=Decimal(100))
        acknowledge(journal, "co-1", ticket=1, now=NOW)
        submit(journal, "co-2", volume=Decimal(3), price=Decimal(50))
        acknowledge(journal, "co-2", ticket=2, now=NOW)

        state = compute_limits_state(
            journal=journal, equity_tracker=AccountEquityTracker(), clock=clock
        )

        assert state.open_notional == Decimal(2 * 100 + 3 * 50)

    def test_open_notional_ignores_orders_with_no_known_price(self) -> None:
        db = BridgeDatabase(":memory:")
        journal = OrderJournal(db)
        clock = FixedClock(NOW_MS)
        submit(journal, "co-1", price=None)
        acknowledge(journal, "co-1", ticket=1, now=NOW)

        state = compute_limits_state(
            journal=journal, equity_tracker=AccountEquityTracker(), clock=clock
        )

        assert state.open_notional == Decimal(0)

    def test_daily_loss_reflects_the_equity_tracker(self) -> None:
        db = BridgeDatabase(":memory:")
        journal = OrderJournal(db)
        clock = FixedClock(NOW_MS)
        tracker = AccountEquityTracker()
        tracker.record(Decimal(100_000))
        tracker.record(Decimal(95_000))

        state = compute_limits_state(journal=journal, equity_tracker=tracker, clock=clock)

        assert state.daily_loss == Decimal(5_000)

    def test_open_notional_falls_back_to_the_recorded_notional_price_bound(self) -> None:
        # A MARKET order (no price of its own) that recorded a fresh-quote
        # bound at submit (audit F2) still counts once acknowledged.
        db = BridgeDatabase(":memory:")
        journal = OrderJournal(db)
        clock = FixedClock(NOW_MS)
        submit(journal, "co-1", volume=Decimal(2), price=None)
        with journal.db.transaction() as conn:
            journal.set_notional_price(
                conn, client_order_id="co-1", notional_price=Decimal(101), now=NOW
            )
        acknowledge(journal, "co-1", ticket=1, now=NOW)

        state = compute_limits_state(
            journal=journal, equity_tracker=AccountEquityTracker(), clock=clock
        )

        assert state.open_notional == Decimal(2 * 101)

    def test_open_notional_counts_an_unknown_entry_with_no_ticket(self) -> None:
        # audit F2's "restart retains pending/UNKNOWN exposure": an UNKNOWN
        # entry with no known ticket must still count, not just tickets
        # already on the books.
        db = BridgeDatabase(":memory:")
        journal = OrderJournal(db)
        clock = FixedClock(NOW_MS)
        submit(journal, "co-1", volume=Decimal(3), price=None)
        with journal.db.transaction() as conn:
            journal.set_notional_price(
                conn, client_order_id="co-1", notional_price=Decimal(50), now=NOW
            )
        mark_unknown(journal, "co-1", now=NOW)

        state = compute_limits_state(
            journal=journal, equity_tracker=AccountEquityTracker(), clock=clock
        )

        assert state.open_notional == Decimal(3 * 50)


class TestComputeLimitsStateStaleness:
    def test_a_tracker_that_never_recorded_anything_is_stale(self) -> None:
        db = BridgeDatabase(":memory:")
        journal = OrderJournal(db)
        clock = FixedClock(NOW_MS)

        state = compute_limits_state(
            journal=journal, equity_tracker=AccountEquityTracker(), clock=clock
        )

        assert state.daily_loss_evidence_stale is True

    def test_a_tracker_with_a_recorded_baseline_is_not_stale_without_an_age_bound(self) -> None:
        db = BridgeDatabase(":memory:")
        journal = OrderJournal(db)
        clock = FixedClock(NOW_MS)
        tracker = AccountEquityTracker()
        tracker.record(Decimal(100_000))  # no `now=`, as most callers never pass one

        state = compute_limits_state(journal=journal, equity_tracker=tracker, clock=clock)

        assert state.daily_loss_evidence_stale is False

    def test_an_account_mismatch_marks_the_evidence_permanently_unavailable(self) -> None:
        db = BridgeDatabase(":memory:")
        journal = OrderJournal(db)
        clock = FixedClock(NOW_MS)
        tracker = AccountEquityTracker()
        tracker.record(Decimal(100_000), now=clock.now_utc())
        tracker.mark_unavailable("persisted baseline belongs to a different account")

        state = compute_limits_state(journal=journal, equity_tracker=tracker, clock=clock)

        assert state.daily_loss_evidence_stale is True

    def test_equity_older_than_the_configured_max_age_is_stale(self) -> None:
        db = BridgeDatabase(":memory:")
        journal = OrderJournal(db)
        clock = FixedClock(NOW_MS)
        tracker = AccountEquityTracker()
        tracker.record(Decimal(100_000), now=clock.now_utc())
        clock.advance(31)

        state = compute_limits_state(
            journal=journal,
            equity_tracker=tracker,
            clock=clock,
            equity_max_age_seconds=30,
        )

        assert state.daily_loss_evidence_stale is True

    def test_equity_within_the_configured_max_age_is_not_stale(self) -> None:
        db = BridgeDatabase(":memory:")
        journal = OrderJournal(db)
        clock = FixedClock(NOW_MS)
        tracker = AccountEquityTracker()
        tracker.record(Decimal(100_000), now=clock.now_utc())
        clock.advance(10)

        state = compute_limits_state(
            journal=journal,
            equity_tracker=tracker,
            clock=clock,
            equity_max_age_seconds=30,
        )

        assert state.daily_loss_evidence_stale is False
