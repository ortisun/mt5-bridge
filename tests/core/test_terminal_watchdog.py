"""Terminal watchdog: connection-loss detection and bounded-backoff
reconnection (plan §114 UTM VM Operations), never assuming order state
during an outage."""

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import cast

import pytest

from mt5_bridge.core.metrics_port import NullMetrics
from mt5_bridge.core.terminal_watchdog import (
    TerminalWatchdogState,
    WatchdogBackoffPolicy,
    watchdog_tick,
)
from mt5_bridge.mt5.client import Mt5Client
from mt5_bridge.mt5.fake_client import FakeMt5Client
from mt5_bridge.mt5.models import (
    AccountInfo,
    AccountTradeMode,
    Mt5Credentials,
    PositionAccounting,
    TerminalInfo,
)

from testkit.clock import FixedClock

_NOW = datetime(2026, 1, 1, tzinfo=UTC)
_BACKOFF = WatchdogBackoffPolicy(base_seconds=1.0, max_seconds=8.0)


def _no_op_reconnect(client: Mt5Client) -> None:
    del client


def _reconnect_via_fake(client: Mt5Client) -> None:
    cast(FakeMt5Client, client).simulate_reconnect()


def _account() -> AccountInfo:
    return AccountInfo(
        login=1,
        balance=Decimal(0),
        equity=Decimal(0),
        margin=Decimal(0),
        margin_free=Decimal(0),
        currency="BRL",
        position_accounting=PositionAccounting.NETTING,
        trade_allowed=True,
        server="demo",
        trade_mode=AccountTradeMode.DEMO,
    )


def _connected_client() -> FakeMt5Client:
    client = FakeMt5Client(symbols=(), account=_account(), clock=FixedClock(0))
    client.connect(Mt5Credentials(login=1, password="x", server="demo"))
    return client


class TestWatchdogBackoffPolicy:
    def test_delay_doubles_and_is_capped(self) -> None:
        assert _BACKOFF.delay(1) == timedelta(seconds=1)
        assert _BACKOFF.delay(2) == timedelta(seconds=2)
        assert _BACKOFF.delay(3) == timedelta(seconds=4)
        assert _BACKOFF.delay(4) == timedelta(seconds=8)
        assert _BACKOFF.delay(10) == timedelta(seconds=8)

    def test_rejects_a_non_positive_or_inverted_bound(self) -> None:
        with pytest.raises(ValueError):
            WatchdogBackoffPolicy(base_seconds=0, max_seconds=1)
        with pytest.raises(ValueError):
            WatchdogBackoffPolicy(base_seconds=2, max_seconds=1)


class SpyMetrics(NullMetrics):
    def __init__(self) -> None:
        self.increments: list[str] = []

    def increment(self, name: str, value: int = 1, /, **attributes: object) -> None:
        del value, attributes
        self.increments.append(name)


class TestWatchdogTick:
    def test_a_disconnect_records_a_stream_gap_and_a_reconnect_records_a_reconnect(self) -> None:
        state = TerminalWatchdogState()
        client = _connected_client()
        client.simulate_disconnect()
        metrics = SpyMetrics()

        watchdog_tick(
            state, client, reconnect=_no_op_reconnect, now=_NOW, backoff=_BACKOFF, metrics=metrics
        )
        assert metrics.increments == ["mt5_bridge.stream.gap"]

        watchdog_tick(
            state,
            client,
            reconnect=_reconnect_via_fake,
            now=_NOW + timedelta(seconds=2),
            backoff=_BACKOFF,
            metrics=metrics,
        )
        assert metrics.increments == ["mt5_bridge.stream.gap", "mt5_bridge.stream.reconnect"]

    def test_no_transition_records_nothing(self) -> None:
        state = TerminalWatchdogState()
        client = _connected_client()
        metrics = SpyMetrics()

        watchdog_tick(
            state, client, reconnect=_no_op_reconnect, now=_NOW, backoff=_BACKOFF, metrics=metrics
        )

        assert metrics.increments == []

    def test_a_connected_terminal_stays_connected_and_reports_no_change(self) -> None:
        state = TerminalWatchdogState()
        client = _connected_client()

        changed = watchdog_tick(
            state,
            client,
            reconnect=_no_op_reconnect,
            now=_NOW,
            backoff=_BACKOFF,
            metrics=NullMetrics(),
        )

        assert changed is False
        assert state.connected is True

    def test_a_dropped_terminal_is_detected_and_reported_once(self) -> None:
        state = TerminalWatchdogState()
        client = _connected_client()
        client.simulate_disconnect()

        changed = watchdog_tick(
            state,
            client,
            reconnect=_no_op_reconnect,
            now=_NOW,
            backoff=_BACKOFF,
            metrics=NullMetrics(),
        )

        assert changed is True
        assert state.connected is False
        # A second tick with nothing changed must not re-report the transition.
        changed_again = watchdog_tick(
            state,
            client,
            reconnect=_no_op_reconnect,
            now=_NOW,
            backoff=_BACKOFF,
            metrics=NullMetrics(),
        )
        assert changed_again is False

    def test_a_successful_reconnect_attempt_restores_connectivity(self) -> None:
        state = TerminalWatchdogState()
        client = _connected_client()
        client.simulate_disconnect()

        # First tick: the drop is detected and reported; the reconnect
        # attempt itself does not yet succeed (the terminal process is
        # still down), so `state.connected` stays observable as False.
        first = watchdog_tick(
            state,
            client,
            reconnect=_no_op_reconnect,
            now=_NOW,
            backoff=_BACKOFF,
            metrics=NullMetrics(),
        )
        assert first is True
        assert state.connected is False

        # Once the terminal comes back and a later tick's reconnect attempt
        # succeeds, the recovery is reported as its own transition.
        second = watchdog_tick(
            state,
            client,
            reconnect=_reconnect_via_fake,
            now=_NOW + timedelta(seconds=2),
            backoff=_BACKOFF,
            metrics=NullMetrics(),
        )
        assert second is True
        assert state.connected is True
        assert state.attempt == 0
        assert state.next_attempt_at is None

    def test_a_failed_reconnect_attempt_schedules_a_bounded_backoff_retry(self) -> None:
        state = TerminalWatchdogState()
        client = _connected_client()
        client.simulate_disconnect()

        def failing_reconnect(client: Mt5Client) -> None:
            del client
            raise RuntimeError("terminal still unreachable")

        watchdog_tick(
            state,
            client,
            reconnect=failing_reconnect,
            now=_NOW,
            backoff=_BACKOFF,
            metrics=NullMetrics(),
        )
        assert state.connected is False
        assert state.attempt == 1
        assert state.next_attempt_at == _NOW + timedelta(seconds=1)

        # Before the scheduled retry, no new attempt is made (attempt count
        # stays put) even though the terminal is still disconnected.
        watchdog_tick(
            state,
            client,
            reconnect=failing_reconnect,
            now=_NOW + timedelta(milliseconds=500),
            backoff=_BACKOFF,
            metrics=NullMetrics(),
        )
        assert state.attempt == 1

        # Once the backoff window elapses, a new attempt is made and its
        # own failure schedules a longer retry.
        watchdog_tick(
            state,
            client,
            reconnect=failing_reconnect,
            now=_NOW + timedelta(seconds=2),
            backoff=_BACKOFF,
            metrics=NullMetrics(),
        )
        assert state.attempt == 2
        assert state.next_attempt_at == _NOW + timedelta(seconds=2) + timedelta(seconds=2)

    def test_a_probe_that_raises_is_treated_as_disconnected(self) -> None:
        state = TerminalWatchdogState()

        class _RaisingClient(FakeMt5Client):
            def terminal_info(self) -> TerminalInfo:
                raise RuntimeError("terminal probe failed")

        client = _RaisingClient(symbols=(), account=_account(), clock=FixedClock(0))

        changed = watchdog_tick(
            state,
            client,
            reconnect=_no_op_reconnect,
            now=_NOW,
            backoff=_BACKOFF,
            metrics=NullMetrics(),
        )

        assert changed is True
        assert state.connected is False
