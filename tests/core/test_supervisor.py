"""Boot netting-mode verification and the dead-man switch sweep."""

from datetime import UTC, datetime
from decimal import Decimal

import pytest

from mt5_bridge.core.database import BridgeDatabase
from mt5_bridge.core.dead_man_switch import DeadManSwitch
from mt5_bridge.core.kill_switch import KillSwitch, KillSwitchState
from mt5_bridge.core.metrics_port import NullMetrics
from mt5_bridge.core.supervisor import (
    AccountBindingMismatch,
    NettingModeMismatch,
    sweep_dead_man_switch,
    verify_account_binding,
    verify_netting_mode,
)
from mt5_bridge.mt5.fake_client import FakeMt5Client
from mt5_bridge.mt5.models import (
    AccountTradeMode,
    FillingMode,
    Mt5Credentials,
    NewOrderIntent,
    OrderSide,
    OrderType,
    PositionAccounting,
    TimeInForce,
)

from testkit.clock import FixedClock
from testkit.fixtures import WINZ25, default_account, default_symbols

NOW_MS = 1_700_000_000_000
NOW = datetime.fromtimestamp(NOW_MS / 1000, tz=UTC)


def connected_client(trade_mode: AccountTradeMode = AccountTradeMode.DEMO) -> FakeMt5Client:
    client = FakeMt5Client(
        symbols=default_symbols(),
        account=default_account(trade_mode=trade_mode),
        clock=FixedClock(NOW_MS),
    )
    client.connect(Mt5Credentials(login=1, password="x", server="Demo"))
    return client


class TestVerifyNettingMode:
    def test_a_matching_mode_passes_silently(self) -> None:
        client = connected_client()

        verify_netting_mode(client, expected=PositionAccounting.NETTING)

    def test_a_mismatched_mode_raises(self) -> None:
        client = connected_client()

        try:
            verify_netting_mode(client, expected=PositionAccounting.HEDGING)
        except NettingModeMismatch as error:
            assert error.expected is PositionAccounting.HEDGING
            assert error.actual is PositionAccounting.NETTING
        else:
            raise AssertionError("expected NettingModeMismatch")


class TestVerifyAccountBinding:
    def test_the_expected_demo_account_passes(self) -> None:
        client = connected_client()

        verify_account_binding(
            client, environment="demo", expected_login=12345678, expected_server="Demo"
        )

    def test_paper_also_requires_a_demo_account(self) -> None:
        client = connected_client(AccountTradeMode.REAL)

        with pytest.raises(AccountBindingMismatch, match="trade mode"):
            verify_account_binding(
                client, environment="paper", expected_login=None, expected_server=None
            )

    def test_a_different_login_is_refused(self) -> None:
        client = connected_client()

        with pytest.raises(AccountBindingMismatch, match="login"):
            verify_account_binding(
                client, environment="demo", expected_login=999, expected_server=None
            )

    def test_a_different_server_is_refused(self) -> None:
        client = connected_client()

        with pytest.raises(AccountBindingMismatch, match="server"):
            verify_account_binding(
                client, environment="demo", expected_login=None, expected_server="Other-REAL"
            )

    def test_live_refuses_a_demo_account(self) -> None:
        client = connected_client()

        with pytest.raises(AccountBindingMismatch, match="trade mode"):
            verify_account_binding(
                client, environment="live", expected_login=None, expected_server=None
            )

    def test_live_accepts_a_real_account(self) -> None:
        client = connected_client(AccountTradeMode.REAL)

        verify_account_binding(
            client, environment="live", expected_login=None, expected_server=None
        )

    def test_an_unknown_environment_fails_closed(self) -> None:
        client = connected_client()

        with pytest.raises(AccountBindingMismatch, match="environment"):
            verify_account_binding(
                client, environment="staging", expected_login=None, expected_server=None
            )


class TestSweepDeadManSwitch:
    def test_a_switch_that_is_not_expired_does_nothing(self) -> None:
        client = connected_client()
        clock = FixedClock(NOW_MS)
        switch = DeadManSwitch(timeout_seconds=30)
        switch.heartbeat(now_ms=NOW_MS)
        kill_switch = KillSwitch(
            BridgeDatabase(":memory:"), now=clock.now_utc(), metrics=NullMetrics()
        )

        result = sweep_dead_man_switch(
            switch=switch, kill_switch=kill_switch, client=client, clock=clock
        )

        assert result.expired is False
        assert kill_switch.status().state is KillSwitchState.NORMAL

    def test_an_expired_switch_cancels_all_open_orders_and_engages_the_kill_switch(self) -> None:
        client = connected_client()
        clock = FixedClock(NOW_MS)
        switch = DeadManSwitch(timeout_seconds=30)
        switch.heartbeat(now_ms=NOW_MS)
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
                comment="dm",
            )
        )
        assert sent.order is not None
        clock.advance(31)
        kill_switch = KillSwitch(
            BridgeDatabase(":memory:"), now=clock.now_utc(), metrics=NullMetrics()
        )

        result = sweep_dead_man_switch(
            switch=switch, kill_switch=kill_switch, client=client, clock=clock
        )

        assert result.expired is True
        assert result.cancel_report is not None
        assert sent.order in result.cancel_report.canceled
        assert kill_switch.status().state is KillSwitchState.CANCELED_ALL
        assert client.orders() == ()
