"""OrderJournal: idempotent receive, terminal-result recording, UNKNOWN."""

from datetime import UTC, datetime
from decimal import Decimal

from mt5_bridge.core.database import BridgeDatabase
from mt5_bridge.core.oms_state import BridgeOrderState
from mt5_bridge.core.order_journal import JournalEntry, OrderJournal, ReceiveOutcome
from mt5_bridge.mt5.models import OrderSide, OrderType, TimeInForce

NOW = datetime(2024, 1, 1, tzinfo=UTC)
LATER = datetime(2024, 1, 1, 0, 0, 5, tzinfo=UTC)


def make_journal() -> OrderJournal:
    return OrderJournal(BridgeDatabase(":memory:"))


def receive(
    journal: OrderJournal,
    client_order_id: str = "co-1",
    payload_hash: str = "hash-a",
    *,
    symbol: str = "WINZ25",
    now: datetime = NOW,
) -> tuple[ReceiveOutcome, JournalEntry]:
    with journal.db.transaction() as conn:
        return journal.begin_receive(
            conn,
            client_order_id=client_order_id,
            payload_hash=payload_hash,
            symbol=symbol,
            side=OrderSide.BUY,
            order_type=OrderType.MARKET,
            volume=Decimal(1),
            price=None,
            time_in_force=TimeInForce.DAY,
            magic=42,
            now=now,
        )


class TestBeginReceive:
    def test_new_client_order_id_is_inserted_as_received(self) -> None:
        journal = make_journal()

        outcome, entry = receive(journal)

        assert outcome is ReceiveOutcome.NEW
        assert entry.state is BridgeOrderState.RECEIVED
        assert entry.client_order_id == "co-1"

    def test_replay_with_the_same_payload_returns_the_existing_entry(self) -> None:
        journal = make_journal()
        receive(journal)

        outcome, entry = receive(journal)

        assert outcome is ReceiveOutcome.DUPLICATE_SAME_PAYLOAD
        assert entry.state is BridgeOrderState.RECEIVED

    def test_replay_with_a_different_payload_is_a_conflict(self) -> None:
        journal = make_journal()
        receive(journal, payload_hash="hash-a")

        outcome, entry = receive(journal, payload_hash="hash-b")

        assert outcome is ReceiveOutcome.CONFLICT
        assert entry.payload_hash == "hash-a"


class TestRecordResult:
    def test_record_result_updates_state_ticket_and_retcode(self) -> None:
        journal = make_journal()
        receive(journal)

        with journal.db.transaction() as conn:
            journal.record_result(
                conn,
                client_order_id="co-1",
                state=BridgeOrderState.ACKNOWLEDGED,
                ticket=1001,
                mt5_retcode=10008,
                mt5_last_error=None,
                reason=None,
                now=LATER,
            )

        entry = journal.get_no_transaction("co-1")
        assert entry is not None
        assert entry.state is BridgeOrderState.ACKNOWLEDGED
        assert entry.ticket == 1001
        assert entry.updated_at == LATER

    def test_mark_unknown_records_state_and_reason(self) -> None:
        journal = make_journal()
        receive(journal)

        with journal.db.transaction() as conn:
            journal.mark_unknown(conn, client_order_id="co-1", reason="lost response", now=LATER)

        entry = journal.get_no_transaction("co-1")
        assert entry is not None
        assert entry.state is BridgeOrderState.UNKNOWN
        assert entry.reason == "lost response"


class TestQueries:
    def test_get_returns_none_for_an_unknown_id(self) -> None:
        journal = make_journal()

        assert journal.get_no_transaction("missing") is None

    def test_list_unconfirmed_excludes_terminal_and_acknowledged_entries(self) -> None:
        journal = make_journal()
        receive(journal, client_order_id="co-received")
        receive(journal, client_order_id="co-acked")
        with journal.db.transaction() as conn:
            journal.record_result(
                conn,
                client_order_id="co-acked",
                state=BridgeOrderState.ACKNOWLEDGED,
                ticket=2,
                mt5_retcode=10008,
                mt5_last_error=None,
                reason=None,
                now=LATER,
            )

        unconfirmed = journal.list_unconfirmed_no_transaction()

        ids = {entry.client_order_id for entry in unconfirmed}
        assert ids == {"co-received"}


class TestCountCreatedSince:
    def test_counts_only_entries_created_at_or_after_the_cutoff(self) -> None:
        journal = make_journal()
        receive(journal, client_order_id="co-old", now=datetime(2024, 1, 1, 0, 0, 0, tzinfo=UTC))
        receive(journal, client_order_id="co-new", now=datetime(2024, 1, 1, 0, 1, 0, tzinfo=UTC))

        count = journal.count_created_since_no_transaction(
            since=datetime(2024, 1, 1, 0, 0, 30, tzinfo=UTC)
        )

        assert count == 1

    def test_a_cutoff_before_everything_counts_everything(self) -> None:
        journal = make_journal()
        receive(journal, client_order_id="co-1", now=NOW)
        receive(journal, client_order_id="co-2", now=NOW)

        count = journal.count_created_since_no_transaction(since=datetime(2023, 1, 1, tzinfo=UTC))

        assert count == 2


class TestNotionalPrice:
    def test_a_fresh_entry_has_no_notional_price(self) -> None:
        journal = make_journal()
        receive(journal)

        entry = journal.get_no_transaction("co-1")

        assert entry is not None
        assert entry.notional_price is None

    def test_set_notional_price_persists_the_bound(self) -> None:
        journal = make_journal()
        receive(journal)

        with journal.db.transaction() as conn:
            journal.set_notional_price(
                conn, client_order_id="co-1", notional_price=Decimal("101.5"), now=LATER
            )

        entry = journal.get_no_transaction("co-1")
        assert entry is not None
        assert entry.notional_price == Decimal("101.5")
        assert entry.updated_at == LATER

    def test_setting_the_notional_price_does_not_disturb_the_order_price(self) -> None:
        journal = make_journal()
        receive(journal)  # price=None, as a MARKET order submits it

        with journal.db.transaction() as conn:
            journal.set_notional_price(
                conn, client_order_id="co-1", notional_price=Decimal(101), now=LATER
            )

        entry = journal.get_no_transaction("co-1")
        assert entry is not None
        assert entry.price is None
        assert entry.notional_price == Decimal(101)


class TestListOpenExposure:
    def test_a_received_entry_with_no_ticket_still_counts_as_exposure(self) -> None:
        journal = make_journal()
        receive(journal, client_order_id="co-1")

        exposure = journal.list_open_exposure_no_transaction()

        assert {entry.client_order_id for entry in exposure} == {"co-1"}

    def test_an_unknown_entry_with_no_ticket_still_counts_as_exposure(self) -> None:
        journal = make_journal()
        receive(journal, client_order_id="co-1")
        with journal.db.transaction() as conn:
            journal.mark_unknown(conn, client_order_id="co-1", reason="lost response", now=LATER)

        exposure = journal.list_open_exposure_no_transaction()

        assert {entry.client_order_id for entry in exposure} == {"co-1"}

    def test_a_terminal_entry_does_not_count_as_exposure(self) -> None:
        journal = make_journal()
        receive(journal, client_order_id="co-1")
        with journal.db.transaction() as conn:
            journal.record_result(
                conn,
                client_order_id="co-1",
                state=BridgeOrderState.REJECTED,
                ticket=None,
                mt5_retcode=None,
                mt5_last_error=None,
                reason="rejected",
                now=LATER,
            )

        exposure = journal.list_open_exposure_no_transaction()

        assert exposure == ()


class TestHasUnknownForSymbol:
    def test_no_entries_at_all_is_false(self) -> None:
        journal = make_journal()

        assert journal.has_unknown_for_symbol_no_transaction("WINZ25") is False

    def test_an_unknown_entry_for_the_symbol_is_true(self) -> None:
        journal = make_journal()
        receive(journal, client_order_id="co-1", symbol="WINZ25")
        with journal.db.transaction() as conn:
            journal.mark_unknown(conn, client_order_id="co-1", reason="lost response", now=LATER)

        assert journal.has_unknown_for_symbol_no_transaction("WINZ25") is True

    def test_an_unknown_entry_for_a_different_symbol_does_not_count(self) -> None:
        journal = make_journal()
        receive(journal, client_order_id="co-1", symbol="PETR4")
        with journal.db.transaction() as conn:
            journal.mark_unknown(conn, client_order_id="co-1", reason="lost response", now=LATER)

        assert journal.has_unknown_for_symbol_no_transaction("WINZ25") is False

    def test_a_resolved_entry_no_longer_counts(self) -> None:
        journal = make_journal()
        receive(journal, client_order_id="co-1", symbol="WINZ25")
        with journal.db.transaction() as conn:
            journal.mark_unknown(conn, client_order_id="co-1", reason="lost response", now=LATER)
            journal.record_result(
                conn,
                client_order_id="co-1",
                state=BridgeOrderState.ACKNOWLEDGED,
                ticket=5,
                mt5_retcode=10008,
                mt5_last_error=None,
                reason="resolved manually",
                now=LATER,
            )

        assert journal.has_unknown_for_symbol_no_transaction("WINZ25") is False
