"""OrderService: idempotent submit, cancel, cancel-replace, amend.

Covers the plan §114 M8 DoD behaviors: idempotent POST by client_order_id,
cancel-replace under concurrent fill / new ticket / rejected second leg /
lost response, kill-switch and LimitsGuard blocking before any terminal
call, and dated-futures-only / filling-mode-per-symbol enforcement.
"""

import asyncio
from collections.abc import Awaitable, Callable
from decimal import Decimal
from pathlib import Path

import pytest

from mt5_bridge.core.clock_drift import ClockDriftState
from mt5_bridge.core.database import BridgeDatabase
from mt5_bridge.core.kill_switch import KillSwitch, KillSwitchAction
from mt5_bridge.core.limits_guard import LimitsConfig, LimitsGuard, LimitsState
from mt5_bridge.core.limits_state_provider import AccountEquityTracker, compute_limits_state
from mt5_bridge.core.metrics_port import Metrics, NullMetrics
from mt5_bridge.core.oms_state import BridgeOrderState, ReplaceOutcome
from mt5_bridge.core.order_journal import OrderJournal
from mt5_bridge.core.order_service import (
    ClockDriftExceeded,
    IdempotencyConflict,
    OrderNotFound,
    OrderService,
    ReplaceOrderSpec,
    SubmitResult,
    select_filling_mode,
)
from mt5_bridge.core.replace_operations import ReplaceOperationStore
from mt5_bridge.core.sequence_log import SequenceLog
from mt5_bridge.core.terminal_watchdog import TerminalWatchdogState
from mt5_bridge.core.worker import BridgeWorker
from mt5_bridge.mt5.errors import Mt5ConnectionLost
from mt5_bridge.mt5.fake_client import FakeMt5Client
from mt5_bridge.mt5.models import (
    FillingMode,
    Mt5Credentials,
    Mt5Deal,
    OrderSide,
    OrderType,
    Tick,
    TimeInForce,
)
from mt5_bridge.mt5.retcodes import UnrecognizedRetcode

from testkit.clock import FixedClock
from testkit.fixtures import PETR4, WIN_CONTINUOUS, WINZ25, default_account, default_symbols

MAGIC = 555
PERMISSIVE_STATE = LimitsState(
    open_notional=Decimal(0), daily_loss=Decimal(0), orders_in_last_minute=0
)


def no_quote(_symbol: str) -> None:
    """A ``symbol_tick`` replacement that reports no quote at all, for
    ``monkeypatch.setattr``."""
    return None


def permissive_limits_guard() -> LimitsGuard:
    return LimitsGuard(
        LimitsConfig(
            symbol_allowlist=None,
            max_volume_per_order=Decimal(1000),
            max_orders_per_minute=1000,
            max_open_notional=Decimal(10_000_000),
            max_daily_loss=Decimal(10_000_000),
        )
    )


def make_service(
    *,
    clock: FixedClock | None = None,
    limits_guard: LimitsGuard | None = None,
    limits_state_provider: Callable[[], LimitsState] | None = None,
    clock_drift_state: ClockDriftState | None = None,
    clock_drift_threshold_seconds: float = 2.0,
    terminal_watchdog_state: TerminalWatchdogState | None = None,
    metrics: Metrics | None = None,
) -> tuple[OrderService, FakeMt5Client, KillSwitch]:
    clock = clock or FixedClock(1_700_000_000_000)
    db = BridgeDatabase(":memory:")
    client = FakeMt5Client(symbols=default_symbols(), account=default_account(), clock=clock)
    client.connect(Mt5Credentials(login=1, password="x", server="Demo"))
    client.set_tick(
        WINZ25, bid=Decimal(100), ask=Decimal(101), last=Decimal(100), volume=Decimal(1)
    )
    resolved_metrics = metrics or NullMetrics()
    kill_switch = KillSwitch(db, now=clock.now_utc(), metrics=resolved_metrics)
    service = OrderService(
        db=db,
        journal=OrderJournal(db),
        replace_operations=ReplaceOperationStore(db),
        sequence_log=SequenceLog(db),
        limits_guard=limits_guard or permissive_limits_guard(),
        kill_switch=kill_switch,
        worker=BridgeWorker(client),
        clock=clock,
        magic=MAGIC,
        limits_state_provider=limits_state_provider or (lambda: PERMISSIVE_STATE),
        clock_drift_state=clock_drift_state or ClockDriftState(drift_seconds=0.0),
        clock_drift_threshold_seconds=clock_drift_threshold_seconds,
        metrics=resolved_metrics,
        terminal_watchdog_state=terminal_watchdog_state,
    )
    return service, client, kill_switch


async def run_worker[T](service: OrderService, awaitable: Awaitable[T]) -> T:
    """Drive ``awaitable`` to completion, servicing worker commands as they land.

    The worker thread is never started in these tests: everything stays on
    one thread and one event loop, deterministically.
    """
    task: asyncio.Task[T] = asyncio.ensure_future(awaitable)
    while not task.done():
        service.worker.drain()
        await asyncio.sleep(0)
    return task.result()


class TestIdempotentSubmit:
    @pytest.mark.asyncio
    async def test_a_new_market_order_is_submitted_and_filled(self) -> None:
        service, _client, _kill_switch = make_service()

        result = await run_worker(
            service,
            service.submit_order(
                client_order_id="co-1",
                symbol=WINZ25,
                side=OrderSide.BUY,
                order_type=OrderType.MARKET,
                volume=Decimal(1),
                time_in_force=TimeInForce.DAY,
            ),
        )

        assert result.replayed is False
        assert result.entry.state is BridgeOrderState.FILLED
        assert result.entry.ticket is not None

    @pytest.mark.asyncio
    async def test_replaying_the_same_payload_does_not_create_a_second_order(self) -> None:
        service, client, _kill_switch = make_service()

        def submit():
            return service.submit_order(
                client_order_id="co-2",
                symbol=WINZ25,
                side=OrderSide.BUY,
                order_type=OrderType.MARKET,
                volume=Decimal(1),
                time_in_force=TimeInForce.DAY,
            )

        first = await run_worker(service, submit())

        second = await run_worker(service, submit())

        assert second.replayed is True
        assert second.entry.ticket == first.entry.ticket
        assert len(client.history_orders(0, 2_000_000_000)) == 1

    @pytest.mark.asyncio
    async def test_a_different_payload_with_the_same_id_conflicts(self) -> None:
        service, _client, _kill_switch = make_service()
        await run_worker(
            service,
            service.submit_order(
                client_order_id="co-3",
                symbol=WINZ25,
                side=OrderSide.BUY,
                order_type=OrderType.MARKET,
                volume=Decimal(1),
                time_in_force=TimeInForce.DAY,
            ),
        )

        with pytest.raises(IdempotencyConflict):
            await run_worker(
                service,
                service.submit_order(
                    client_order_id="co-3",
                    symbol=WINZ25,
                    side=OrderSide.SELL,
                    order_type=OrderType.MARKET,
                    volume=Decimal(2),
                    time_in_force=TimeInForce.DAY,
                ),
            )


class TestSymbolRulesAreEnforcedByTheService:
    @pytest.mark.asyncio
    async def test_a_continuous_series_symbol_is_rejected_before_any_terminal_send(self) -> None:
        service, client, _kill_switch = make_service()
        client.set_tick(
            WIN_CONTINUOUS, bid=Decimal(100), ask=Decimal(101), last=Decimal(100), volume=Decimal(1)
        )

        result = await run_worker(
            service,
            service.submit_order(
                client_order_id="co-continuous",
                symbol=WIN_CONTINUOUS,
                side=OrderSide.BUY,
                order_type=OrderType.MARKET,
                volume=Decimal(1),
                time_in_force=TimeInForce.DAY,
            ),
        )

        assert result.entry.state is BridgeOrderState.REJECTED
        assert result.entry.ticket is None

    @pytest.mark.asyncio
    async def test_filling_mode_is_read_from_the_symbol_never_hardcoded(self) -> None:
        assert select_filling_mode(frozenset({FillingMode.IOC})) is FillingMode.IOC
        assert (
            select_filling_mode(frozenset({FillingMode.RETURN, FillingMode.IOC}))
            is FillingMode.RETURN
        )
        assert select_filling_mode(frozenset()) is None


class TestUnpricedMarketOrdersAreBounded:
    """Audit F2 reproduction and fix: a MARKET order carries no price, so the
    guard used to see zero notional for it no matter the real quote."""

    @pytest.mark.asyncio
    async def test_an_unpriced_market_order_cannot_exceed_max_open_notional(self) -> None:
        # Exact audit reproduction: volume 1, fake ask 101, max_open_notional=1
        # used to return FILLED; it must now be rejected.
        strict_guard = LimitsGuard(
            LimitsConfig(
                symbol_allowlist=None,
                max_volume_per_order=Decimal(1000),
                max_orders_per_minute=1000,
                max_open_notional=Decimal(1),
                max_daily_loss=Decimal(10_000_000),
            )
        )
        service, client, _kill_switch = make_service(limits_guard=strict_guard)

        result = await run_worker(
            service,
            service.submit_order(
                client_order_id="co-unpriced-1",
                symbol=WINZ25,
                side=OrderSide.BUY,
                order_type=OrderType.MARKET,
                volume=Decimal(1),
                time_in_force=TimeInForce.DAY,
            ),
        )

        assert result.entry.state is BridgeOrderState.REJECTED
        assert "max_open_notional" in (result.entry.reason or "")
        assert client.orders() == ()

    @pytest.mark.asyncio
    async def test_a_market_sell_is_bounded_by_the_bid_not_the_ask(self) -> None:
        strict_guard = LimitsGuard(
            LimitsConfig(
                symbol_allowlist=None,
                max_volume_per_order=Decimal(1000),
                max_orders_per_minute=1000,
                max_open_notional=Decimal(1),
                max_daily_loss=Decimal(10_000_000),
            )
        )
        service, _client, _kill_switch = make_service(limits_guard=strict_guard)

        result = await run_worker(
            service,
            service.submit_order(
                client_order_id="co-unpriced-sell",
                symbol=WINZ25,
                side=OrderSide.SELL,
                order_type=OrderType.MARKET,
                volume=Decimal(1),
                time_in_force=TimeInForce.DAY,
            ),
        )

        assert result.entry.state is BridgeOrderState.REJECTED
        assert "max_open_notional" in (result.entry.reason or "")

    @pytest.mark.asyncio
    async def test_a_missing_quote_is_rejected_before_any_terminal_send(self) -> None:
        # PETR4 never gets `set_tick` in `make_service` (only WINZ25 does).
        service, client, _kill_switch = make_service()
        result = await run_worker(
            service,
            service.submit_order(
                client_order_id="co-no-quote",
                symbol=PETR4,
                side=OrderSide.BUY,
                order_type=OrderType.MARKET,
                volume=Decimal(1),
                time_in_force=TimeInForce.DAY,
            ),
        )

        assert result.entry.state is BridgeOrderState.REJECTED
        assert "quote" in (result.entry.reason or "")
        assert client.orders() == ()

    @pytest.mark.asyncio
    async def test_a_stale_quote_is_rejected_before_any_terminal_send(self) -> None:
        clock = FixedClock(1_700_000_000_000)
        service, client, _kill_switch = make_service(clock=clock)
        client.set_tick(
            WINZ25, bid=Decimal(100), ask=Decimal(101), last=Decimal(100), volume=Decimal(1)
        )
        clock.advance(6)  # past OrderService's default 5s max quote age

        result = await run_worker(
            service,
            service.submit_order(
                client_order_id="co-stale-quote",
                symbol=WINZ25,
                side=OrderSide.BUY,
                order_type=OrderType.MARKET,
                volume=Decimal(1),
                time_in_force=TimeInForce.DAY,
            ),
        )

        assert result.entry.state is BridgeOrderState.REJECTED
        assert "old" in (result.entry.reason or "")
        assert client.orders() == ()

    @pytest.mark.asyncio
    async def test_a_fresh_enough_quote_is_accepted(self) -> None:
        clock = FixedClock(1_700_000_000_000)
        service, _client, _kill_switch = make_service(clock=clock)

        result = await run_worker(
            service,
            service.submit_order(
                client_order_id="co-fresh-quote",
                symbol=WINZ25,
                side=OrderSide.BUY,
                order_type=OrderType.MARKET,
                volume=Decimal(1),
                time_in_force=TimeInForce.DAY,
            ),
        )

        assert result.entry.state is BridgeOrderState.FILLED

    @pytest.mark.asyncio
    async def test_a_tick_current_at_terminal_read_is_not_future_dated_by_queue_wait(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        clock = FixedClock(1_700_000_000_000)
        received_at = clock.now_utc()
        service, client, _kill_switch = make_service(clock=clock)
        original_symbol_tick = client.symbol_tick

        def read_current_tick(symbol: str) -> Tick | None:
            clock.advance(0.001)
            client.set_tick(
                symbol, bid=Decimal(100), ask=Decimal(101), last=Decimal(100), volume=Decimal(1)
            )
            return original_symbol_tick(symbol)

        monkeypatch.setattr(client, "symbol_tick", read_current_tick)

        result = await run_worker(
            service,
            service.submit_order(
                client_order_id="co-current-after-read",
                symbol=WINZ25,
                side=OrderSide.BUY,
                order_type=OrderType.MARKET,
                volume=Decimal(1),
                time_in_force=TimeInForce.DAY,
            ),
        )

        assert result.entry.state is BridgeOrderState.FILLED
        assert result.entry.created_at == received_at

    @pytest.mark.asyncio
    async def test_a_tick_after_the_terminal_read_time_is_rejected(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        clock = FixedClock(1_700_000_000_000)
        service, client, _kill_switch = make_service(clock=clock)

        def read_future_tick(symbol: str) -> Tick:
            clock.advance(0.001)
            return Tick(
                symbol=symbol,
                time_msc=clock.now_ms() + 1,
                bid=Decimal(100),
                ask=Decimal(101),
                last=Decimal(100),
                volume=Decimal(1),
            )

        monkeypatch.setattr(client, "symbol_tick", read_future_tick)

        result = await run_worker(
            service,
            service.submit_order(
                client_order_id="co-future-after-read",
                symbol=WINZ25,
                side=OrderSide.BUY,
                order_type=OrderType.MARKET,
                volume=Decimal(1),
                time_in_force=TimeInForce.DAY,
            ),
        )

        assert result.entry.state is BridgeOrderState.REJECTED
        assert "future" in (result.entry.reason or "")
        assert client.positions() == ()

    @pytest.mark.asyncio
    async def test_restart_retains_unknown_exposure_from_an_unpriced_market_order(
        self, tmp_path: Path
    ) -> None:
        db_path = str(tmp_path / "bridge.db")
        clock = FixedClock(1_700_000_000_000)
        db = BridgeDatabase(db_path)
        client = FakeMt5Client(symbols=default_symbols(), account=default_account(), clock=clock)
        client.connect(Mt5Credentials(login=1, password="x", server="Demo"))
        client.set_tick(
            WINZ25, bid=Decimal(100), ask=Decimal(101), last=Decimal(100), volume=Decimal(1)
        )
        client.queue_order_send_result(Mt5ConnectionLost("simulated lost response"))
        journal = OrderJournal(db)
        service = OrderService(
            db=db,
            journal=journal,
            replace_operations=ReplaceOperationStore(db),
            sequence_log=SequenceLog(db),
            limits_guard=permissive_limits_guard(),
            kill_switch=KillSwitch(db, now=clock.now_utc(), metrics=NullMetrics()),
            worker=BridgeWorker(client),
            clock=clock,
            magic=MAGIC,
            limits_state_provider=lambda: PERMISSIVE_STATE,
            clock_drift_state=ClockDriftState(drift_seconds=0.0),
            clock_drift_threshold_seconds=2.0,
            metrics=NullMetrics(),
        )
        submitted = await run_worker(
            service,
            service.submit_order(
                client_order_id="co-restart-1",
                symbol=WINZ25,
                side=OrderSide.BUY,
                order_type=OrderType.MARKET,
                volume=Decimal(4),
                time_in_force=TimeInForce.DAY,
            ),
        )
        assert submitted.entry.state is BridgeOrderState.UNKNOWN
        db.close()

        # Simulate a restart: a fresh database connection, a fresh journal,
        # and a fresh (never-recorded) equity tracker over the same file.
        reopened_journal = OrderJournal(BridgeDatabase(db_path))
        state = compute_limits_state(
            journal=reopened_journal, equity_tracker=AccountEquityTracker(), clock=clock
        )

        assert state.open_notional == Decimal(4 * 101)


class TestKillSwitchAndLimitsBlockLocally:
    @pytest.mark.asyncio
    async def test_kill_switch_halted_rejects_without_a_terminal_round_trip(self) -> None:
        service, client, kill_switch = make_service()
        clock = FixedClock(1_700_000_000_000)
        kill_switch.activate(KillSwitchAction.HALT_NEW_ORDERS, reason="test", now=clock.now_utc())

        result = await run_worker(
            service,
            service.submit_order(
                client_order_id="co-halted",
                symbol=WINZ25,
                side=OrderSide.BUY,
                order_type=OrderType.MARKET,
                volume=Decimal(1),
                time_in_force=TimeInForce.DAY,
            ),
        )

        assert result.entry.state is BridgeOrderState.REJECTED
        assert result.entry.reason == "kill switch engaged"
        assert client.orders() == ()

    @pytest.mark.asyncio
    async def test_limits_guard_rejection_rejects_without_a_terminal_round_trip(self) -> None:
        strict_guard = LimitsGuard(
            LimitsConfig(
                symbol_allowlist=frozenset(),
                max_volume_per_order=Decimal(1000),
                max_orders_per_minute=1000,
                max_open_notional=Decimal(10_000_000),
                max_daily_loss=Decimal(10_000_000),
            )
        )
        service, client, _kill_switch = make_service(limits_guard=strict_guard)

        result = await run_worker(
            service,
            service.submit_order(
                client_order_id="co-limited",
                symbol=WINZ25,
                side=OrderSide.BUY,
                order_type=OrderType.MARKET,
                volume=Decimal(1),
                time_in_force=TimeInForce.DAY,
            ),
        )

        assert result.entry.state is BridgeOrderState.REJECTED
        assert client.orders() == ()


class SpyMetrics(NullMetrics):
    def __init__(self) -> None:
        self.durations: list[tuple[str, float, dict[str, object]]] = []
        self.increments: list[tuple[str, dict[str, object]]] = []

    def record_duration(self, name: str, seconds: float, /, **attributes: object) -> None:
        self.durations.append((name, seconds, attributes))

    def increment(self, name: str, value: int = 1, /, **attributes: object) -> None:
        del value
        self.increments.append((name, attributes))


class TestSubmitOrderMetrics:
    """plan §84: submit->ack latency, order rejections by reason, UNKNOWN orders."""

    @pytest.mark.asyncio
    async def test_a_rejection_records_duration_and_a_rejected_counter(self) -> None:
        metrics = SpyMetrics()
        service, _client, kill_switch = make_service(metrics=metrics)
        kill_switch.activate(
            KillSwitchAction.HALT_NEW_ORDERS, reason="test", now=FixedClock(0).now_utc()
        )
        metrics.increments.clear()

        result = await run_worker(
            service,
            service.submit_order(
                client_order_id="co-metrics-rejected",
                symbol=WINZ25,
                side=OrderSide.BUY,
                order_type=OrderType.MARKET,
                volume=Decimal(1),
                time_in_force=TimeInForce.DAY,
            ),
        )

        assert result.entry.state is BridgeOrderState.REJECTED
        (duration_name, _seconds, duration_attrs) = metrics.durations[0]
        assert duration_name == "mt5_bridge.order.submit_ack.duration"
        assert duration_attrs == {"symbol": WINZ25}
        assert metrics.increments == [
            ("mt5_bridge.order.rejected", {"reason": "kill switch engaged", "symbol": WINZ25})
        ]

    @pytest.mark.asyncio
    async def test_an_unrecognized_send_code_records_the_unknown_counter(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        metrics = SpyMetrics()
        service, client, _kill_switch = make_service(metrics=metrics)

        def send(intent: object) -> object:
            raise UnrecognizedRetcode(10099, "order_send")

        monkeypatch.setattr(client, "order_send", send)
        result = await run_worker(
            service,
            service.submit_order(
                client_order_id="co-metrics-unknown",
                symbol=WINZ25,
                side=OrderSide.BUY,
                order_type=OrderType.LIMIT,
                volume=Decimal(1),
                price=Decimal(99),
                time_in_force=TimeInForce.DAY,
            ),
        )

        assert result.entry.state is BridgeOrderState.UNKNOWN
        assert metrics.increments == [("mt5_bridge.order.unknown", {"symbol": WINZ25})]

    @pytest.mark.asyncio
    async def test_a_filled_order_records_no_rejection_or_unknown_counter(self) -> None:
        metrics = SpyMetrics()
        service, _client, _kill_switch = make_service(metrics=metrics)

        result = await run_worker(
            service,
            service.submit_order(
                client_order_id="co-metrics-filled",
                symbol=WINZ25,
                side=OrderSide.BUY,
                order_type=OrderType.MARKET,
                volume=Decimal(1),
                time_in_force=TimeInForce.DAY,
            ),
        )

        assert result.entry.state is BridgeOrderState.FILLED
        assert metrics.increments == []
        assert len(metrics.durations) == 1


class TestTerminalWatchdogBlocksSubmission:
    @pytest.mark.asyncio
    async def test_a_disconnected_terminal_rejects_without_a_terminal_round_trip(self) -> None:
        watchdog_state = TerminalWatchdogState(connected=False)
        service, client, _kill_switch = make_service(terminal_watchdog_state=watchdog_state)

        result = await run_worker(
            service,
            service.submit_order(
                client_order_id="co-disconnected",
                symbol=WINZ25,
                side=OrderSide.BUY,
                order_type=OrderType.MARKET,
                volume=Decimal(1),
                time_in_force=TimeInForce.DAY,
            ),
        )

        assert result.entry.state is BridgeOrderState.REJECTED
        assert result.entry.reason == "terminal disconnected"
        assert client.orders() == ()

    @pytest.mark.asyncio
    async def test_a_disconnected_terminal_still_blocks_an_emergency_reduction(self) -> None:
        watchdog_state = TerminalWatchdogState(connected=False)
        service, client, _kill_switch = make_service(terminal_watchdog_state=watchdog_state)

        result = await run_worker(
            service,
            service.submit_order(
                client_order_id="co-emergency-disconnected",
                symbol=WINZ25,
                side=OrderSide.SELL,
                order_type=OrderType.MARKET,
                volume=Decimal(1),
                time_in_force=TimeInForce.DAY,
                emergency_reduction=True,
            ),
        )

        assert result.entry.state is BridgeOrderState.REJECTED
        assert result.entry.reason == "terminal disconnected"
        assert client.orders() == ()

    @pytest.mark.asyncio
    async def test_a_connected_terminal_is_not_blocked(self) -> None:
        watchdog_state = TerminalWatchdogState(connected=True)
        service, _client, _kill_switch = make_service(terminal_watchdog_state=watchdog_state)

        result = await run_worker(
            service,
            service.submit_order(
                client_order_id="co-connected",
                symbol=WINZ25,
                side=OrderSide.BUY,
                order_type=OrderType.MARKET,
                volume=Decimal(1),
                time_in_force=TimeInForce.DAY,
            ),
        )

        assert result.entry.state is BridgeOrderState.FILLED


class TestClockDriftBlocksSubmission:
    @pytest.mark.asyncio
    async def test_drift_within_threshold_does_not_block(self) -> None:
        service, _client, _kill_switch = make_service(
            clock_drift_state=ClockDriftState(drift_seconds=1.0),
            clock_drift_threshold_seconds=2.0,
        )

        result = await run_worker(
            service,
            service.submit_order(
                client_order_id="co-drift-ok",
                symbol=WINZ25,
                side=OrderSide.BUY,
                order_type=OrderType.MARKET,
                volume=Decimal(1),
                time_in_force=TimeInForce.DAY,
            ),
        )

        assert result.entry.state is BridgeOrderState.FILLED

    @pytest.mark.asyncio
    async def test_drift_past_the_threshold_rejects_without_a_terminal_round_trip(self) -> None:
        service, client, _kill_switch = make_service(
            clock_drift_state=ClockDriftState(drift_seconds=5.0),
            clock_drift_threshold_seconds=2.0,
        )

        result = await run_worker(
            service,
            service.submit_order(
                client_order_id="co-drift-bad",
                symbol=WINZ25,
                side=OrderSide.BUY,
                order_type=OrderType.MARKET,
                volume=Decimal(1),
                time_in_force=TimeInForce.DAY,
            ),
        )

        assert result.entry.state is BridgeOrderState.REJECTED
        assert "drift" in (result.entry.reason or "")
        assert client.orders() == ()

    @pytest.mark.asyncio
    async def test_negative_drift_past_the_threshold_also_rejects(self) -> None:
        service, _client, _kill_switch = make_service(
            clock_drift_state=ClockDriftState(drift_seconds=-5.0),
            clock_drift_threshold_seconds=2.0,
        )

        result = await run_worker(
            service,
            service.submit_order(
                client_order_id="co-drift-negative",
                symbol=WINZ25,
                side=OrderSide.BUY,
                order_type=OrderType.MARKET,
                volume=Decimal(1),
                time_in_force=TimeInForce.DAY,
            ),
        )

        assert result.entry.state is BridgeOrderState.REJECTED

    @pytest.mark.asyncio
    async def test_replace_order_raises_when_drift_exceeds_the_threshold(self) -> None:
        permissive_service, _client, _kill_switch = make_service()
        submitted = await run_worker(
            permissive_service,
            permissive_service.submit_order(
                client_order_id="co-orig",
                symbol=WINZ25,
                side=OrderSide.BUY,
                order_type=OrderType.LIMIT,
                price=Decimal(50),
                volume=Decimal(1),
                time_in_force=TimeInForce.DAY,
            ),
        )
        assert submitted.entry.state is BridgeOrderState.ACKNOWLEDGED
        # Rebuild a service against the same pieces but with bad drift, to
        # prove replace_order itself refuses before touching the original.
        drifting_service, _client2, _ks2 = make_service(
            clock_drift_state=ClockDriftState(drift_seconds=9.0), clock_drift_threshold_seconds=2.0
        )

        with pytest.raises(ClockDriftExceeded):
            await run_worker(
                drifting_service,
                drifting_service.replace_order(
                    operation_id="op-drift",
                    original_client_order_id="co-orig",
                    new_client_order_id="co-new",
                    new_order=ReplaceOrderSpec(
                        symbol=WINZ25,
                        side=OrderSide.BUY,
                        order_type=OrderType.LIMIT,
                        price=Decimal(60),
                        volume=Decimal(1),
                        time_in_force=TimeInForce.DAY,
                    ),
                ),
            )


class TestUnknownBlocksSameInstrument:
    @pytest.mark.asyncio
    async def test_an_unknown_order_blocks_a_new_order_on_the_same_symbol(self) -> None:
        service, client, _kill_switch = make_service()
        client.queue_order_send_result(Mt5ConnectionLost("simulated lost response"))
        first = await run_worker(
            service,
            service.submit_order(
                client_order_id="co-unknown",
                symbol=WINZ25,
                side=OrderSide.BUY,
                order_type=OrderType.MARKET,
                volume=Decimal(1),
                time_in_force=TimeInForce.DAY,
            ),
        )
        assert first.entry.state is BridgeOrderState.UNKNOWN

        second = await run_worker(
            service,
            service.submit_order(
                client_order_id="co-blocked",
                symbol=WINZ25,
                side=OrderSide.BUY,
                order_type=OrderType.MARKET,
                volume=Decimal(1),
                time_in_force=TimeInForce.DAY,
            ),
        )

        assert second.entry.state is BridgeOrderState.REJECTED
        assert "UNKNOWN" in (second.entry.reason or "")
        assert client.orders() == ()

    @pytest.mark.asyncio
    async def test_an_unknown_order_does_not_block_a_different_symbol(self) -> None:
        service, client, _kill_switch = make_service()
        client.queue_order_send_result(Mt5ConnectionLost("simulated lost response"))
        first = await run_worker(
            service,
            service.submit_order(
                client_order_id="co-unknown-2",
                symbol=WINZ25,
                side=OrderSide.BUY,
                order_type=OrderType.MARKET,
                volume=Decimal(1),
                time_in_force=TimeInForce.DAY,
            ),
        )
        assert first.entry.state is BridgeOrderState.UNKNOWN
        client.set_tick(
            PETR4, bid=Decimal(30), ask=Decimal(30), last=Decimal(30), volume=Decimal(1)
        )

        second = await run_worker(
            service,
            service.submit_order(
                client_order_id="co-other-symbol",
                symbol=PETR4,
                side=OrderSide.BUY,
                order_type=OrderType.MARKET,
                volume=Decimal(100),
                time_in_force=TimeInForce.DAY,
            ),
        )

        assert second.entry.state is BridgeOrderState.FILLED


class TestCancelOrder:
    @pytest.mark.asyncio
    async def test_canceling_a_pending_order_succeeds(self) -> None:
        service, _client, _kill_switch = make_service()
        submitted = await run_worker(
            service,
            service.submit_order(
                client_order_id="co-cancel",
                symbol=WINZ25,
                side=OrderSide.BUY,
                order_type=OrderType.LIMIT,
                price=Decimal(50),
                volume=Decimal(1),
                time_in_force=TimeInForce.DAY,
            ),
        )
        assert submitted.entry.state is BridgeOrderState.ACKNOWLEDGED

        canceled = await run_worker(service, service.cancel_order("co-cancel"))

        assert canceled.state is BridgeOrderState.CANCELED

    @pytest.mark.asyncio
    async def test_canceling_an_unknown_id_raises_not_found(self) -> None:
        service, _client, _kill_switch = make_service()

        with pytest.raises(OrderNotFound):
            await run_worker(service, service.cancel_order("nope"))

    @pytest.mark.asyncio
    async def test_cancel_after_a_concurrent_fill_reports_the_true_filled_state(self) -> None:
        service, client, _kill_switch = make_service()
        submitted = await run_worker(
            service,
            service.submit_order(
                client_order_id="co-race",
                symbol=WINZ25,
                side=OrderSide.BUY,
                order_type=OrderType.LIMIT,
                price=Decimal(50),
                volume=Decimal(1),
                time_in_force=TimeInForce.DAY,
            ),
        )
        assert submitted.entry.ticket is not None
        client.force_fill(submitted.entry.ticket)

        canceled = await run_worker(service, service.cancel_order("co-race"))

        assert canceled.state is BridgeOrderState.FILLED


class TestReplaceOrder:
    async def _submit_resting(
        self, service: OrderService, client: FakeMt5Client, client_order_id: str
    ) -> SubmitResult:
        return await run_worker(
            service,
            service.submit_order(
                client_order_id=client_order_id,
                symbol=WINZ25,
                side=OrderSide.BUY,
                order_type=OrderType.LIMIT,
                price=Decimal(50),
                volume=Decimal(1),
                time_in_force=TimeInForce.DAY,
            ),
        )

    @pytest.mark.asyncio
    async def test_replace_gets_a_new_ticket_distinct_from_the_original(self) -> None:
        service, client, _kill_switch = make_service()
        original = await self._submit_resting(service, client, "co-orig-1")

        result = await run_worker(
            service,
            service.replace_order(
                operation_id="op-1",
                original_client_order_id="co-orig-1",
                new_client_order_id="co-new-1",
                new_order=ReplaceOrderSpec(
                    symbol=WINZ25,
                    side=OrderSide.BUY,
                    order_type=OrderType.LIMIT,
                    price=Decimal(60),
                    volume=Decimal(1),
                    time_in_force=TimeInForce.DAY,
                ),
            ),
        )

        assert result.outcome is ReplaceOutcome.REPLACED
        assert result.original is not None
        assert result.new is not None
        assert result.original.state is BridgeOrderState.CANCELED
        assert result.new.state is BridgeOrderState.ACKNOWLEDGED
        assert result.new.ticket != original.entry.ticket

    @pytest.mark.asyncio
    async def test_replace_when_the_new_leg_is_rejected_keeps_the_original_canceled(self) -> None:
        service, client, _kill_switch = make_service()
        await self._submit_resting(service, client, "co-orig-2")

        result = await run_worker(
            service,
            service.replace_order(
                operation_id="op-2",
                original_client_order_id="co-orig-2",
                new_client_order_id="co-new-2",
                new_order=ReplaceOrderSpec(
                    symbol=PETR4,
                    side=OrderSide.BUY,
                    order_type=OrderType.LIMIT,
                    price=Decimal(30),
                    volume=Decimal(1),  # below PETR4's min volume of 100 -> rejected
                    time_in_force=TimeInForce.DAY,
                ),
            ),
        )

        assert result.outcome is ReplaceOutcome.CANCELED_NEW_REJECTED
        assert result.original is not None
        assert result.new is not None
        assert result.original.state is BridgeOrderState.CANCELED
        assert result.new.state is BridgeOrderState.REJECTED

    @pytest.mark.asyncio
    async def test_replace_with_a_concurrent_fill_does_not_send_a_new_leg(self) -> None:
        service, client, _kill_switch = make_service()
        original = await self._submit_resting(service, client, "co-orig-3")
        assert original.entry.ticket is not None
        client.force_fill(original.entry.ticket)

        result = await run_worker(
            service,
            service.replace_order(
                operation_id="op-3",
                original_client_order_id="co-orig-3",
                new_client_order_id="co-new-3",
                new_order=ReplaceOrderSpec(
                    symbol=WINZ25,
                    side=OrderSide.BUY,
                    order_type=OrderType.LIMIT,
                    price=Decimal(60),
                    volume=Decimal(1),
                    time_in_force=TimeInForce.DAY,
                ),
            ),
        )

        assert result.outcome is ReplaceOutcome.CANCEL_FAILED
        assert result.original is not None
        assert result.original.state is BridgeOrderState.FILLED
        assert result.new is None

    @pytest.mark.asyncio
    async def test_replace_when_the_worker_is_unavailable_resolves_as_cancel_failed(self) -> None:
        # Unlike a lost response (Mt5ConnectionLost -> UNKNOWN below): a
        # command the worker never even dispatched leaves no ambiguity, so
        # the original leg is left exactly as it was, and only the replace
        # operation itself resolves — never a dangling PENDING row.
        service, client, _kill_switch = make_service()
        original = await self._submit_resting(service, client, "co-orig-worker-unavail")
        service.worker.stop()

        result = await service.replace_order(
            operation_id="op-worker-unavail",
            original_client_order_id="co-orig-worker-unavail",
            new_client_order_id="co-new-worker-unavail",
            new_order=ReplaceOrderSpec(
                symbol=WINZ25,
                side=OrderSide.BUY,
                order_type=OrderType.LIMIT,
                price=Decimal(60),
                volume=Decimal(1),
                time_in_force=TimeInForce.DAY,
            ),
        )

        assert result.outcome is ReplaceOutcome.CANCEL_FAILED
        assert result.original is not None
        assert result.original.state is BridgeOrderState.ACKNOWLEDGED
        assert result.original.ticket == original.entry.ticket
        assert result.new is None

    @pytest.mark.asyncio
    async def test_replace_with_a_lost_cancel_response_is_unknown(self) -> None:
        service, client, _kill_switch = make_service()
        await self._submit_resting(service, client, "co-orig-4")
        client.queue_cancel_result(Mt5ConnectionLost("simulated lost response"))

        result = await run_worker(
            service,
            service.replace_order(
                operation_id="op-4",
                original_client_order_id="co-orig-4",
                new_client_order_id="co-new-4",
                new_order=ReplaceOrderSpec(
                    symbol=WINZ25,
                    side=OrderSide.BUY,
                    order_type=OrderType.LIMIT,
                    price=Decimal(60),
                    volume=Decimal(1),
                    time_in_force=TimeInForce.DAY,
                ),
            ),
        )

        assert result.outcome is ReplaceOutcome.UNKNOWN
        assert result.original is not None
        assert result.original.state is BridgeOrderState.UNKNOWN
        assert result.new is None

    @pytest.mark.asyncio
    async def test_replace_with_an_unrecognized_cancel_code_is_unknown_not_pending(
        self,
    ) -> None:
        service, client, _kill_switch = make_service()
        await self._submit_resting(service, client, "co-orig-rc")
        client.queue_cancel_result(UnrecognizedRetcode(10099, "order_send(remove)"))

        result = await run_worker(
            service,
            service.replace_order(
                operation_id="op-rc",
                original_client_order_id="co-orig-rc",
                new_client_order_id="co-new-rc",
                new_order=ReplaceOrderSpec(
                    symbol=WINZ25,
                    side=OrderSide.BUY,
                    order_type=OrderType.LIMIT,
                    price=Decimal(60),
                    volume=Decimal(1),
                    time_in_force=TimeInForce.DAY,
                ),
            ),
        )

        assert result.outcome is ReplaceOutcome.UNKNOWN
        assert result.original is not None
        assert result.original.state is BridgeOrderState.UNKNOWN
        assert result.original.mt5_retcode == 10099

    @pytest.mark.asyncio
    async def test_repeating_the_same_operation_id_returns_the_stored_outcome(self) -> None:
        service, client, _kill_switch = make_service()
        await self._submit_resting(service, client, "co-orig-5")
        spec = ReplaceOrderSpec(
            symbol=WINZ25,
            side=OrderSide.BUY,
            order_type=OrderType.LIMIT,
            price=Decimal(60),
            volume=Decimal(1),
            time_in_force=TimeInForce.DAY,
        )
        first = await run_worker(
            service,
            service.replace_order(
                operation_id="op-5",
                original_client_order_id="co-orig-5",
                new_client_order_id="co-new-5",
                new_order=spec,
            ),
        )

        second = await run_worker(
            service,
            service.replace_order(
                operation_id="op-5",
                original_client_order_id="co-orig-5",
                new_client_order_id="co-new-5",
                new_order=spec,
            ),
        )

        assert second.outcome == first.outcome
        assert first.new is not None
        assert second.new is not None
        assert second.new.ticket == first.new.ticket
        assert len(client.history_orders(0, 2_000_000_000)) == 2  # not resubmitted a third time

    @pytest.mark.asyncio
    async def test_replace_after_a_partial_fill_submits_only_the_remaining_volume(self) -> None:
        service, client, _kill_switch = make_service()
        original = await run_worker(
            service,
            service.submit_order(
                client_order_id="co-orig-7",
                symbol=WINZ25,
                side=OrderSide.BUY,
                order_type=OrderType.LIMIT,
                price=Decimal(50),
                volume=Decimal(5),
                time_in_force=TimeInForce.DAY,
            ),
        )
        assert original.entry.ticket is not None
        client.partial_fill(original.entry.ticket, Decimal(2))

        result = await run_worker(
            service,
            service.replace_order(
                operation_id="op-7",
                original_client_order_id="co-orig-7",
                new_client_order_id="co-new-7",
                new_order=ReplaceOrderSpec(
                    symbol=WINZ25,
                    side=OrderSide.BUY,
                    order_type=OrderType.LIMIT,
                    price=Decimal(60),
                    volume=Decimal(5),  # same desired total as the original request
                    time_in_force=TimeInForce.DAY,
                ),
            ),
        )

        assert result.outcome is ReplaceOutcome.REPLACED
        assert result.original is not None
        assert result.original.state is BridgeOrderState.CANCELED
        assert result.new is not None
        assert result.new.volume == Decimal(3)  # 5 desired - 2 already filled

    @pytest.mark.asyncio
    async def test_replace_of_an_unfilled_resting_order_asks_for_the_full_new_volume(
        self,
    ) -> None:
        # 075 demo reproducer: the terminal's cancel result reports no dealt
        # volume, which must not be read as "the whole original filled".
        service, client, _kill_switch = make_service()
        await self._submit_resting(service, client, "co-orig-8")

        result = await run_worker(
            service,
            service.replace_order(
                operation_id="op-8",
                original_client_order_id="co-orig-8",
                new_client_order_id="co-new-8",
                new_order=ReplaceOrderSpec(
                    symbol=WINZ25,
                    side=OrderSide.BUY,
                    order_type=OrderType.LIMIT,
                    price=Decimal(60),
                    volume=Decimal(1),
                    time_in_force=TimeInForce.DAY,
                ),
            ),
        )

        assert result.outcome is ReplaceOutcome.REPLACED
        assert result.new is not None
        assert result.new.volume == Decimal(1)

    @pytest.mark.asyncio
    async def test_replace_whose_desired_total_is_already_filled_submits_nothing(
        self,
    ) -> None:
        service, client, _kill_switch = make_service()
        original = await run_worker(
            service,
            service.submit_order(
                client_order_id="co-orig-9",
                symbol=WINZ25,
                side=OrderSide.BUY,
                order_type=OrderType.LIMIT,
                price=Decimal(50),
                volume=Decimal(5),
                time_in_force=TimeInForce.DAY,
            ),
        )
        assert original.entry.ticket is not None
        client.partial_fill(original.entry.ticket, Decimal(3))

        result = await run_worker(
            service,
            service.replace_order(
                operation_id="op-9",
                original_client_order_id="co-orig-9",
                new_client_order_id="co-new-9",
                new_order=ReplaceOrderSpec(
                    symbol=WINZ25,
                    side=OrderSide.BUY,
                    order_type=OrderType.LIMIT,
                    price=Decimal(60),
                    volume=Decimal(3),
                    time_in_force=TimeInForce.DAY,
                ),
            ),
        )

        assert result.outcome is ReplaceOutcome.CANCELED_NEW_REJECTED
        assert result.original is not None
        assert result.original.state is BridgeOrderState.CANCELED
        assert result.new is None
        assert len(client.history_orders(0, 2_000_000_000)) == 1  # nothing new sent

    @pytest.mark.asyncio
    async def test_replace_that_cannot_read_the_fills_after_the_cancel_submits_nothing(
        self,
    ) -> None:
        service, client, _kill_switch = make_service()
        await self._submit_resting(service, client, "co-orig-10")

        def lost(date_from_epoch: int, date_to_epoch: int) -> tuple[Mt5Deal, ...]:
            raise Mt5ConnectionLost("terminal is not connected")

        client.history_deals = lost  # type: ignore[method-assign]

        result = await run_worker(
            service,
            service.replace_order(
                operation_id="op-10",
                original_client_order_id="co-orig-10",
                new_client_order_id="co-new-10",
                new_order=ReplaceOrderSpec(
                    symbol=WINZ25,
                    side=OrderSide.BUY,
                    order_type=OrderType.LIMIT,
                    price=Decimal(60),
                    volume=Decimal(1),
                    time_in_force=TimeInForce.DAY,
                ),
            ),
        )

        assert result.outcome is ReplaceOutcome.CANCELED_NEW_REJECTED
        assert result.original is not None
        assert result.original.state is BridgeOrderState.CANCELED
        assert result.new is None
        assert len(client.history_orders(0, 2_000_000_000)) == 1  # nothing new sent

    @pytest.mark.asyncio
    async def test_a_different_payload_with_the_same_operation_id_conflicts(self) -> None:
        service, client, _kill_switch = make_service()
        await self._submit_resting(service, client, "co-orig-6")
        await run_worker(
            service,
            service.replace_order(
                operation_id="op-6",
                original_client_order_id="co-orig-6",
                new_client_order_id="co-new-6",
                new_order=ReplaceOrderSpec(
                    symbol=WINZ25,
                    side=OrderSide.BUY,
                    order_type=OrderType.LIMIT,
                    price=Decimal(60),
                    volume=Decimal(1),
                    time_in_force=TimeInForce.DAY,
                ),
            ),
        )

        with pytest.raises(IdempotencyConflict):
            await run_worker(
                service,
                service.replace_order(
                    operation_id="op-6",
                    original_client_order_id="co-orig-6",
                    new_client_order_id="co-new-6-b",
                    new_order=ReplaceOrderSpec(
                        symbol=WINZ25,
                        side=OrderSide.BUY,
                        order_type=OrderType.LIMIT,
                        price=Decimal(70),
                        volume=Decimal(1),
                        time_in_force=TimeInForce.DAY,
                    ),
                ),
            )


class TestCancelAll:
    @pytest.mark.asyncio
    async def test_cancel_all_cancels_every_open_order_and_updates_the_journal(self) -> None:
        service, client, _kill_switch = make_service()
        first = await run_worker(
            service,
            service.submit_order(
                client_order_id="co-ca-1",
                symbol=WINZ25,
                side=OrderSide.BUY,
                order_type=OrderType.LIMIT,
                price=Decimal(50),
                volume=Decimal(1),
                time_in_force=TimeInForce.DAY,
            ),
        )
        second = await run_worker(
            service,
            service.submit_order(
                client_order_id="co-ca-2",
                symbol=WINZ25,
                side=OrderSide.BUY,
                order_type=OrderType.LIMIT,
                price=Decimal(55),
                volume=Decimal(1),
                time_in_force=TimeInForce.DAY,
            ),
        )

        result = await run_worker(service, service.cancel_all())

        assert set(result.canceled) == {first.entry.ticket, second.entry.ticket}
        assert set(result.journal_updated) == {"co-ca-1", "co-ca-2"}
        assert client.orders() == ()  # nothing left open at the terminal either


class TestFlatten:
    @pytest.mark.asyncio
    async def test_flatten_submits_one_closing_market_order_per_open_position(self) -> None:
        service, client, _kill_switch = make_service()
        await run_worker(
            service,
            service.submit_order(
                client_order_id="co-fl-1",
                symbol=WINZ25,
                side=OrderSide.BUY,
                order_type=OrderType.MARKET,
                volume=Decimal(3),
                time_in_force=TimeInForce.DAY,
            ),
        )
        assert client.positions()  # the market buy opened a position

        result = await run_worker(service, service.flatten())

        assert len(result.submitted) == 1
        closing = result.submitted[0]
        assert closing.entry.state is BridgeOrderState.FILLED
        assert closing.entry.side is OrderSide.SELL
        assert closing.entry.volume == Decimal(3)
        assert client.positions() == ()  # fully closed, never reversed

    @pytest.mark.asyncio
    async def test_flatten_submits_an_exact_buy_reduction_for_a_short_position(self) -> None:
        service, client, _kill_switch = make_service()
        await run_worker(
            service,
            service.submit_order(
                client_order_id="co-fl-short-1",
                symbol=WINZ25,
                side=OrderSide.SELL,
                order_type=OrderType.MARKET,
                volume=Decimal(4),
                time_in_force=TimeInForce.DAY,
            ),
        )
        [position] = client.positions()
        assert position.side is OrderSide.SELL
        assert position.volume == Decimal(4)

        result = await run_worker(service, service.flatten())

        assert len(result.submitted) == 1
        closing = result.submitted[0]
        assert closing.entry.state is BridgeOrderState.FILLED
        assert closing.entry.side is OrderSide.BUY
        assert closing.entry.volume == Decimal(4)
        assert client.positions() == ()

    @pytest.mark.asyncio
    async def test_flatten_still_works_after_it_has_activated_the_kill_switch(self) -> None:
        # Regression: blocks_new_orders() is true for every non-NORMAL state,
        # so activating FLATTEN before calling flatten() must not make
        # flatten()'s own closing order reject itself.
        clock = FixedClock(1_700_000_000_000)
        service, client, kill_switch = make_service(clock=clock)
        await run_worker(
            service,
            service.submit_order(
                client_order_id="co-fl-2",
                symbol=WINZ25,
                side=OrderSide.BUY,
                order_type=OrderType.MARKET,
                volume=Decimal(2),
                time_in_force=TimeInForce.DAY,
            ),
        )
        kill_switch.activate(KillSwitchAction.FLATTEN, reason="test", now=clock.now_utc())
        assert kill_switch.blocks_new_orders() is True

        result = await run_worker(service, service.flatten())

        assert len(result.submitted) == 1
        assert result.submitted[0].entry.state is BridgeOrderState.FILLED
        assert client.positions() == ()


class TestFlattenIsNeverBlockedByExposureLossOrQuoteRules:
    """Plan §107 EmergencyReductionPolicy: hard limits must never block an
    emergency reduction. flatten()'s closing orders pass ``emergency_reduction=
    True``, which must survive exactly the conditions FLATTEN exists to react
    to — an exhausted or unreadable daily-loss figure, an already-full
    open-notional book, and a MARKET order's own fresh-quote requirement —
    while a normal (non-reduction) submission is still rejected in every one
    of those same states.
    """

    def _strict_guard(self, **overrides: object) -> LimitsGuard:
        defaults: dict[str, object] = {
            "symbol_allowlist": None,
            "max_volume_per_order": Decimal(1000),
            "max_orders_per_minute": 1000,
            "max_open_notional": Decimal(1000),
            "max_daily_loss": Decimal(500),
        }
        defaults.update(overrides)
        return LimitsGuard(LimitsConfig(**defaults))  # type: ignore[arg-type]

    async def _open_position(
        self,
        service: OrderService,
        *,
        client_order_id: str = "co-open",
        volume: Decimal = Decimal(2),
    ) -> None:
        opened = await run_worker(
            service,
            service.submit_order(
                client_order_id=client_order_id,
                symbol=WINZ25,
                side=OrderSide.BUY,
                order_type=OrderType.MARKET,
                volume=volume,
                time_in_force=TimeInForce.DAY,
            ),
        )
        assert opened.entry.state is BridgeOrderState.FILLED

    @pytest.mark.asyncio
    async def test_flatten_succeeds_when_daily_loss_already_exceeds_the_cap(self) -> None:
        holder: dict[str, LimitsState] = {"state": PERMISSIVE_STATE}
        service, client, _kill_switch = make_service(
            limits_guard=self._strict_guard(), limits_state_provider=lambda: holder["state"]
        )
        await self._open_position(service)
        holder["state"] = LimitsState(
            open_notional=Decimal(0), daily_loss=Decimal(999_999), orders_in_last_minute=0
        )

        result = await run_worker(service, service.flatten())

        assert len(result.submitted) == 1
        assert result.submitted[0].entry.state is BridgeOrderState.FILLED
        assert client.positions() == ()

    @pytest.mark.asyncio
    async def test_a_normal_order_is_rejected_when_daily_loss_already_exceeds_the_cap(
        self,
    ) -> None:
        state = LimitsState(
            open_notional=Decimal(0), daily_loss=Decimal(999_999), orders_in_last_minute=0
        )
        service, client, _kill_switch = make_service(
            limits_guard=self._strict_guard(), limits_state_provider=lambda: state
        )

        result = await run_worker(
            service,
            service.submit_order(
                client_order_id="co-normal-loss",
                symbol=WINZ25,
                side=OrderSide.BUY,
                order_type=OrderType.MARKET,
                volume=Decimal(1),
                time_in_force=TimeInForce.DAY,
            ),
        )

        assert result.entry.state is BridgeOrderState.REJECTED
        assert "max_daily_loss" in (result.entry.reason or "")
        assert client.orders() == ()

    @pytest.mark.asyncio
    async def test_flatten_succeeds_when_daily_loss_evidence_is_stale(self) -> None:
        holder: dict[str, LimitsState] = {"state": PERMISSIVE_STATE}
        service, client, _kill_switch = make_service(
            limits_guard=self._strict_guard(), limits_state_provider=lambda: holder["state"]
        )
        await self._open_position(service)
        holder["state"] = LimitsState(
            open_notional=Decimal(0),
            daily_loss=Decimal(0),
            orders_in_last_minute=0,
            daily_loss_evidence_stale=True,
        )

        result = await run_worker(service, service.flatten())

        assert len(result.submitted) == 1
        assert result.submitted[0].entry.state is BridgeOrderState.FILLED
        assert client.positions() == ()

    @pytest.mark.asyncio
    async def test_a_normal_order_is_rejected_when_daily_loss_evidence_is_stale(self) -> None:
        state = LimitsState(
            open_notional=Decimal(0),
            daily_loss=Decimal(0),
            orders_in_last_minute=0,
            daily_loss_evidence_stale=True,
        )
        service, client, _kill_switch = make_service(
            limits_guard=self._strict_guard(), limits_state_provider=lambda: state
        )

        result = await run_worker(
            service,
            service.submit_order(
                client_order_id="co-normal-stale-evidence",
                symbol=WINZ25,
                side=OrderSide.BUY,
                order_type=OrderType.MARKET,
                volume=Decimal(1),
                time_in_force=TimeInForce.DAY,
            ),
        )

        assert result.entry.state is BridgeOrderState.REJECTED
        assert "stale" in (result.entry.reason or "")
        assert client.orders() == ()

    @pytest.mark.asyncio
    async def test_flatten_succeeds_when_open_notional_is_already_above_the_cap(self) -> None:
        holder: dict[str, LimitsState] = {"state": PERMISSIVE_STATE}
        service, client, _kill_switch = make_service(
            limits_guard=self._strict_guard(), limits_state_provider=lambda: holder["state"]
        )
        await self._open_position(service)
        holder["state"] = LimitsState(
            open_notional=Decimal(999_999), daily_loss=Decimal(0), orders_in_last_minute=0
        )

        result = await run_worker(service, service.flatten())

        assert len(result.submitted) == 1
        assert result.submitted[0].entry.state is BridgeOrderState.FILLED
        assert client.positions() == ()

    @pytest.mark.asyncio
    async def test_a_normal_order_is_rejected_when_open_notional_is_already_above_the_cap(
        self,
    ) -> None:
        state = LimitsState(
            open_notional=Decimal(999_999), daily_loss=Decimal(0), orders_in_last_minute=0
        )
        service, client, _kill_switch = make_service(
            limits_guard=self._strict_guard(), limits_state_provider=lambda: state
        )

        result = await run_worker(
            service,
            service.submit_order(
                client_order_id="co-normal-notional",
                symbol=WINZ25,
                side=OrderSide.BUY,
                order_type=OrderType.MARKET,
                volume=Decimal(1),
                time_in_force=TimeInForce.DAY,
            ),
        )

        assert result.entry.state is BridgeOrderState.REJECTED
        assert "max_open_notional" in (result.entry.reason or "")
        assert client.orders() == ()

    @pytest.mark.asyncio
    async def test_flatten_succeeds_when_the_quote_is_missing(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        service, client, _kill_switch = make_service()
        await self._open_position(service)
        monkeypatch.setattr(client, "symbol_tick", no_quote)

        result = await run_worker(service, service.flatten())

        assert len(result.submitted) == 1
        assert result.submitted[0].entry.state is BridgeOrderState.FILLED
        assert client.positions() == ()

    @pytest.mark.asyncio
    async def test_a_normal_market_order_is_rejected_when_the_quote_is_missing(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        service, client, _kill_switch = make_service()
        monkeypatch.setattr(client, "symbol_tick", no_quote)

        result = await run_worker(
            service,
            service.submit_order(
                client_order_id="co-normal-no-quote",
                symbol=WINZ25,
                side=OrderSide.BUY,
                order_type=OrderType.MARKET,
                volume=Decimal(1),
                time_in_force=TimeInForce.DAY,
            ),
        )

        assert result.entry.state is BridgeOrderState.REJECTED
        assert "quote" in (result.entry.reason or "")
        assert client.orders() == ()

    @pytest.mark.asyncio
    async def test_flatten_succeeds_when_the_quote_is_stale(self) -> None:
        clock = FixedClock(1_700_000_000_000)
        service, client, _kill_switch = make_service(clock=clock)
        await self._open_position(service)
        clock.advance(6)  # past OrderService's default 5s max quote age

        result = await run_worker(service, service.flatten())

        assert len(result.submitted) == 1
        assert result.submitted[0].entry.state is BridgeOrderState.FILLED
        assert client.positions() == ()

    @pytest.mark.asyncio
    async def test_a_normal_market_order_is_rejected_when_the_quote_is_stale(self) -> None:
        clock = FixedClock(1_700_000_000_000)
        service, client, _kill_switch = make_service(clock=clock)
        clock.advance(6)

        result = await run_worker(
            service,
            service.submit_order(
                client_order_id="co-normal-stale-quote",
                symbol=WINZ25,
                side=OrderSide.BUY,
                order_type=OrderType.MARKET,
                volume=Decimal(1),
                time_in_force=TimeInForce.DAY,
            ),
        )

        assert result.entry.state is BridgeOrderState.REJECTED
        assert "old" in (result.entry.reason or "")
        assert client.orders() == ()


class TestAmendOrder:
    @pytest.mark.asyncio
    async def test_amending_the_price_of_an_open_order_succeeds(self) -> None:
        service, _client, _kill_switch = make_service()
        submitted = await run_worker(
            service,
            service.submit_order(
                client_order_id="co-amend-1",
                symbol=WINZ25,
                side=OrderSide.BUY,
                order_type=OrderType.LIMIT,
                price=Decimal(50),
                volume=Decimal(1),
                time_in_force=TimeInForce.DAY,
            ),
        )
        assert submitted.entry.state is BridgeOrderState.ACKNOWLEDGED

        amended = await run_worker(service, service.amend_order("co-amend-1", price=Decimal(55)))

        assert amended.state is BridgeOrderState.ACKNOWLEDGED
        assert amended.mt5_retcode is not None

    @pytest.mark.asyncio
    async def test_amending_an_unknown_id_raises_not_found(self) -> None:
        service, _client, _kill_switch = make_service()

        with pytest.raises(OrderNotFound):
            await run_worker(service, service.amend_order("nope", price=Decimal(1)))


class TestUnrecognizedTerminalRetcodes:
    """A code outside ``Mt5RetCode`` must never crash a submission mid-journal
    (seen on the demo terminal: the entry stayed RECEIVED, then turned
    UNKNOWN and blocked its symbol)."""

    @pytest.mark.asyncio
    async def test_an_unrecognized_order_check_code_rejects_without_sending(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        service, client, _kill_switch = make_service()

        def check(intent: object) -> object:
            raise UnrecognizedRetcode(10099, "order_check")

        monkeypatch.setattr(client, "order_check", check)
        result = await run_worker(
            service,
            service.submit_order(
                client_order_id="co-rc-1",
                symbol=WINZ25,
                side=OrderSide.BUY,
                order_type=OrderType.LIMIT,
                volume=Decimal(1),
                price=Decimal(99),
                time_in_force=TimeInForce.DAY,
            ),
        )

        assert result.entry.state is BridgeOrderState.REJECTED
        assert result.entry.mt5_retcode == 10099
        assert client.orders() == ()

    @pytest.mark.asyncio
    async def test_an_unrecognized_order_send_code_is_unknown_not_guessed(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        service, client, _kill_switch = make_service()

        def send(intent: object) -> object:
            raise UnrecognizedRetcode(10099, "order_send")

        monkeypatch.setattr(client, "order_send", send)
        result = await run_worker(
            service,
            service.submit_order(
                client_order_id="co-rc-2",
                symbol=WINZ25,
                side=OrderSide.BUY,
                order_type=OrderType.LIMIT,
                volume=Decimal(1),
                price=Decimal(99),
                time_in_force=TimeInForce.DAY,
            ),
        )

        assert result.entry.state is BridgeOrderState.UNKNOWN
        assert result.entry.mt5_retcode == 10099
