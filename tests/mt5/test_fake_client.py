"""Behavioral tests for the deterministic fake terminal.

Every scenario the bridge's core logic depends on (closed-bar semantics,
symbol-rule enforcement, cancel-after-fill races, disconnects) must be
reproducible here without a real terminal, a sleep, or the network.
"""

from decimal import Decimal

import pytest

from mt5_bridge.mt5.fake_client import FakeMt5Client, Mt5ConnectionLost
from mt5_bridge.mt5.models import (
    FillingMode,
    Mt5Credentials,
    Mt5OrderState,
    Mt5RetCode,
    NewOrderIntent,
    OrderSide,
    OrderType,
    Timeframe,
    TimeInForce,
)

from testkit.clock import FixedClock
from testkit.fixtures import (
    PETR4,
    WIN_CONTINUOUS,
    WINZ25,
    default_account,
    default_symbols,
)


def make_client(clock: FixedClock | None = None) -> FakeMt5Client:
    client = FakeMt5Client(
        symbols=default_symbols(),
        account=default_account(),
        clock=clock or FixedClock(1_700_000_000_000),
    )
    client.connect(Mt5Credentials(login=1, password="x", server="Demo"))
    return client


def limit_order(
    symbol: str,
    *,
    price: Decimal,
    volume: Decimal = Decimal(1),
    magic: int = 1,
    comment: str = "t",
) -> NewOrderIntent:
    return NewOrderIntent(
        symbol=symbol,
        side=OrderSide.BUY,
        order_type=OrderType.LIMIT,
        price=price,
        volume=volume,
        time_in_force=TimeInForce.DAY,
        filling_mode=FillingMode.RETURN,
        magic=magic,
        comment=comment,
    )


def market_order(
    symbol: str, *, volume: Decimal = Decimal(1), magic: int = 1, comment: str = "t"
) -> NewOrderIntent:
    return NewOrderIntent(
        symbol=symbol,
        side=OrderSide.BUY,
        order_type=OrderType.MARKET,
        volume=volume,
        time_in_force=TimeInForce.DAY,
        filling_mode=FillingMode.RETURN,
        magic=magic,
        comment=comment,
    )


class TestConnectionLifecycle:
    def test_terminal_reports_connected_after_connect(self) -> None:
        client = make_client()

        assert client.terminal_info().connected is True

    def test_disconnect_then_reconnect_round_trips(self) -> None:
        client = make_client()

        client.simulate_disconnect()
        assert client.terminal_info().connected is False

        client.simulate_reconnect()
        assert client.terminal_info().connected is True

    def test_calls_while_disconnected_raise_connection_lost(self) -> None:
        client = make_client()
        client.simulate_disconnect()

        with pytest.raises(Mt5ConnectionLost):
            client.account_info()


class TestSymbolRulesAreNeverHardcoded:
    def test_symbol_info_returns_the_configured_rules(self) -> None:
        client = make_client()

        info = client.symbol_info(WINZ25)

        assert info is not None
        assert info.volume_step == Decimal(1)
        assert info.is_continuous_series is False

    def test_unknown_symbol_returns_none(self) -> None:
        client = make_client()

        assert client.symbol_info("NOPE") is None


class TestClosedBarsOnly:
    def test_start_pos_zero_includes_the_forming_bar(self) -> None:
        client = make_client()
        client.push_bar(WINZ25, Timeframe.D1, time=1_700_000_000, o=1, h=2, low=1, c=1, volume=10)

        bars = client.copy_bars(WINZ25, Timeframe.D1, start_pos=0, count=1)

        assert len(bars) == 1
        assert bars[0].is_closed is False

    def test_start_pos_one_excludes_the_forming_bar(self) -> None:
        client = make_client()
        client.push_bar(WINZ25, Timeframe.D1, time=1_700_000_000, o=1, h=2, low=1, c=1, volume=10)

        bars = client.copy_bars(WINZ25, Timeframe.D1, start_pos=1, count=5)

        assert bars == ()

    def test_pushing_a_new_bar_closes_the_previous_one(self) -> None:
        client = make_client()
        client.push_bar(WINZ25, Timeframe.D1, time=1_700_000_000, o=1, h=2, low=1, c=1, volume=10)
        client.push_bar(WINZ25, Timeframe.D1, time=1_700_086_400, o=1, h=3, low=1, c=2, volume=20)

        closed = client.copy_bars(WINZ25, Timeframe.D1, start_pos=1, count=5)

        assert len(closed) == 1
        assert closed[0].is_closed is True
        assert closed[0].close == Decimal(1)


class TestOrderSendValidation:
    def test_market_order_fills_immediately_with_a_ticket(self) -> None:
        client = make_client()
        client.set_tick(
            WINZ25, bid=Decimal(100), ask=Decimal(101), last=Decimal(100), volume=Decimal(1)
        )

        result = client.order_send(market_order(WINZ25))

        assert result.retcode is Mt5RetCode.DONE
        assert result.order is not None
        [order] = [o for o in client.history_orders(0, 2_000_000_000) if o.ticket == result.order]
        assert order.state is Mt5OrderState.FILLED
        assert order.price_open == Decimal(101)  # BUY fills at ask

    def test_continuous_series_symbol_is_rejected(self) -> None:
        client = make_client()
        client.set_tick(
            WIN_CONTINUOUS,
            bid=Decimal(100),
            ask=Decimal(101),
            last=Decimal(100),
            volume=Decimal(1),
        )

        result = client.order_send(market_order(WIN_CONTINUOUS))

        assert result.retcode is not Mt5RetCode.DONE
        assert result.order is None

    def test_volume_below_minimum_is_rejected(self) -> None:
        client = make_client()

        result = client.order_send(limit_order(PETR4, price=Decimal("30.00"), volume=Decimal(1)))

        assert result.retcode is Mt5RetCode.INVALID_VOLUME

    def test_unknown_symbol_is_rejected(self) -> None:
        client = make_client()

        result = client.order_send(limit_order("NOPE", price=Decimal(1)))

        assert result.retcode is Mt5RetCode.INVALID
        assert result.order is None


class TestCancelAndConcurrentFill:
    def test_cancel_pending_order_succeeds(self) -> None:
        client = make_client()
        sent = client.order_send(limit_order(WINZ25, price=Decimal(100)))
        assert sent.order is not None

        result = client.cancel_order(sent.order)

        assert result.retcode is Mt5RetCode.DONE

    def test_cancel_after_concurrent_fill_is_rejected(self) -> None:
        client = make_client()
        sent = client.order_send(limit_order(WINZ25, price=Decimal(100)))
        assert sent.order is not None
        client.force_fill(sent.order)

        result = client.cancel_order(sent.order)

        assert result.retcode is Mt5RetCode.REJECT
        orders = client.history_orders(0, 2_000_000_000)
        assert any(o.ticket == sent.order and o.state is Mt5OrderState.FILLED for o in orders)

    def test_cancel_keeps_the_unfilled_volume_on_the_history_order(self) -> None:
        client = make_client()
        sent = client.order_send(limit_order(WINZ25, price=Decimal(100), volume=Decimal(5)))
        assert sent.order is not None

        result = client.cancel_order(sent.order)

        assert result.retcode is Mt5RetCode.DONE
        assert result.volume == Decimal(0)
        history = client.history_orders(0, 2_000_000_000)
        canceled = next(o for o in history if o.ticket == sent.order)
        assert canceled.state is Mt5OrderState.CANCELED
        assert canceled.volume_current == Decimal(5)


class TestPartialFill:
    def test_a_partial_fill_reduces_remaining_volume_and_keeps_the_order_open(self) -> None:
        client = make_client()
        sent = client.order_send(limit_order(WINZ25, price=Decimal(100), volume=Decimal(5)))
        assert sent.order is not None

        client.partial_fill(sent.order, Decimal(2))

        (order,) = client.orders()
        assert order.state is Mt5OrderState.PARTIAL
        assert order.volume_current == Decimal(3)

    def test_canceling_after_a_partial_fill_reports_no_dealt_volume_like_the_terminal(
        self,
    ) -> None:
        # A TRADE_ACTION_REMOVE result carries no dealt volume on the real
        # terminal (075 demo evidence): the unfilled remainder is only on the
        # history order, and the fill itself only in the deals.
        client = make_client()
        sent = client.order_send(limit_order(WINZ25, price=Decimal(100), volume=Decimal(5)))
        assert sent.order is not None
        client.partial_fill(sent.order, Decimal(2))

        result = client.cancel_order(sent.order)

        assert result.retcode is Mt5RetCode.DONE
        assert result.volume == Decimal(0)
        history = client.history_orders(0, 2_000_000_000)
        canceled = next(o for o in history if o.ticket == sent.order)
        assert canceled.volume_current == Decimal(3)

    def test_partial_fill_rejects_more_than_the_remaining_volume(self) -> None:
        client = make_client()
        sent = client.order_send(limit_order(WINZ25, price=Decimal(100), volume=Decimal(5)))
        assert sent.order is not None

        with pytest.raises(ValueError, match="volume_current"):
            client.partial_fill(sent.order, Decimal(6))


class TestInjectableLostResponse:
    def test_queued_exception_is_raised_once_then_drained(self) -> None:
        client = make_client()
        client.set_tick(
            WINZ25, bid=Decimal(100), ask=Decimal(101), last=Decimal(100), volume=Decimal(1)
        )
        client.queue_order_send_result(Mt5ConnectionLost("simulated lost response"))

        with pytest.raises(Mt5ConnectionLost):
            client.order_send(market_order(WINZ25))

        result = client.order_send(market_order(WINZ25))
        assert result.retcode is Mt5RetCode.DONE
