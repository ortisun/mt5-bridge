"""cancel_all_open_orders / flatten_positions: the kill-switch/dead-man payload.

FLATTEN never sizes a reduction beyond the reconciled position, and never
opens or reverses exposure (plan §107 EmergencyReductionPolicy).
"""

from decimal import Decimal

from mt5_bridge.core.emergency_actions import cancel_all_open_orders, flatten_positions
from mt5_bridge.mt5.errors import Mt5ConnectionLost
from mt5_bridge.mt5.fake_client import FakeMt5Client
from mt5_bridge.mt5.models import (
    FillingMode,
    Mt5Credentials,
    Mt5RetCode,
    NewOrderIntent,
    OrderSendResult,
    OrderSide,
    OrderType,
    TimeInForce,
)

from testkit.clock import FixedClock
from testkit.fixtures import WINZ25, default_account, default_symbols


def connected_client() -> FakeMt5Client:
    client = FakeMt5Client(
        symbols=default_symbols(), account=default_account(), clock=FixedClock(1_700_000_000_000)
    )
    client.connect(Mt5Credentials(login=1, password="x", server="Demo"))
    return client


def pending_limit_order(client: FakeMt5Client, *, magic: int = 1) -> int:
    result = client.order_send(
        NewOrderIntent(
            symbol=WINZ25,
            side=OrderSide.BUY,
            order_type=OrderType.LIMIT,
            price=Decimal(100),
            volume=Decimal(1),
            time_in_force=TimeInForce.DAY,
            filling_mode=FillingMode.RETURN,
            magic=magic,
            comment="pending",
        )
    )
    assert result.order is not None
    return result.order


class TestCancelAllOpenOrders:
    def test_cancels_every_open_order_and_reports_it(self) -> None:
        client = connected_client()
        first = pending_limit_order(client)
        second = pending_limit_order(client)

        report = cancel_all_open_orders(client)

        assert set(report.attempted) == {first, second}
        assert set(report.canceled) == {first, second}
        assert report.failed == ()
        assert client.orders() == ()

    def test_a_cancel_the_terminal_rejects_is_reported_as_failed_not_raised(self) -> None:
        client = connected_client()
        ticket = pending_limit_order(client)
        client.queue_cancel_result(
            OrderSendResult(
                retcode=Mt5RetCode.REJECT,
                order=ticket,
                deal=None,
                volume=Decimal(0),
                price=Decimal(0),
                comment="no",
            )
        )

        report = cancel_all_open_orders(client)

        assert report.attempted == (ticket,)
        assert report.failed == (ticket,)
        assert report.canceled == ()

    def test_a_lost_response_while_cancelling_is_reported_as_failed_not_raised(self) -> None:
        client = connected_client()
        ticket = pending_limit_order(client)
        client.queue_cancel_result(Mt5ConnectionLost("simulated lost response"))

        report = cancel_all_open_orders(client)

        assert report.attempted == (ticket,)
        assert report.failed == (ticket,)

    def test_no_open_orders_is_a_no_op_report(self) -> None:
        client = connected_client()

        report = cancel_all_open_orders(client)

        assert report.attempted == ()
        assert report.canceled == ()
        assert report.failed == ()


class TestFlattenPositions:
    def test_no_open_positions_yields_no_intents(self) -> None:
        client = connected_client()

        intents = flatten_positions(client, magic=7, comment="flatten")

        assert intents == ()

    def test_a_long_position_is_closed_by_a_sell_capped_at_its_volume(self) -> None:
        client = connected_client()
        client.set_tick(
            WINZ25, bid=Decimal(100), ask=Decimal(101), last=Decimal(100), volume=Decimal(1)
        )
        client.order_send(
            NewOrderIntent(
                symbol=WINZ25,
                side=OrderSide.BUY,
                order_type=OrderType.MARKET,
                volume=Decimal(3),
                time_in_force=TimeInForce.DAY,
                filling_mode=FillingMode.RETURN,
                magic=1,
                comment="open",
            )
        )

        [intent] = flatten_positions(client, magic=7, comment="flatten")

        assert intent.symbol == WINZ25
        assert intent.side is OrderSide.SELL
        assert intent.volume == Decimal(3)
        assert intent.order_type is OrderType.MARKET

    def test_a_short_position_is_closed_by_a_buy_for_its_exact_volume(self) -> None:
        client = connected_client()
        client.set_tick(
            WINZ25, bid=Decimal(100), ask=Decimal(101), last=Decimal(100), volume=Decimal(1)
        )
        opened = client.order_send(
            NewOrderIntent(
                symbol=WINZ25,
                side=OrderSide.SELL,
                order_type=OrderType.MARKET,
                volume=Decimal(4),
                time_in_force=TimeInForce.DAY,
                filling_mode=FillingMode.RETURN,
                magic=1,
                comment="open short",
            )
        )
        assert opened.retcode is Mt5RetCode.DONE

        [intent] = flatten_positions(client, magic=7, comment="flatten")
        submitted = client.order_send(intent)

        assert intent.side is OrderSide.BUY
        assert intent.volume == Decimal(4)
        assert intent.order_type is OrderType.MARKET
        assert intent.magic == 7
        assert intent.comment == "flatten"
        assert intent.filling_mode is FillingMode.RETURN
        assert intent.time_in_force is TimeInForce.DAY
        assert submitted.retcode is Mt5RetCode.DONE
        assert submitted.volume == Decimal(4)
        assert client.positions() == ()
