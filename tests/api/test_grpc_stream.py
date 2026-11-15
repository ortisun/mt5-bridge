"""The gRPC stream: ping, subscriptions and executions resume as on /v1/stream,
each message the same OCSF event the WebSocket sends."""

from decimal import Decimal
from typing import Any

import grpc
import pytest

from mt5_bridge.api.grpc_api.generated import mt5_bridge_pb2 as pb
from mt5_bridge.api.grpc_api.generated import mt5_bridge_pb2_grpc as pb_grpc
from mt5_bridge.core import ocsf
from mt5_bridge.mt5.models import BookLevel, BookSide, Timeframe

from testkit.app_builder import NOW_MS, build_test_app, build_test_app_state
from testkit.clock import FixedClock
from testkit.fixtures import WINZ25
from testkit.grpc_client import assert_same_ocsf_event, problem_of, read_one, serve_grpc
from testkit.http_client import TypedTestClient

pytestmark = pytest.mark.asyncio

PING = pb.StreamRequest(ping=pb.Ping())


def subscribe(
    channel: pb.Channel.ValueType,
    *,
    symbols: tuple[str, ...] = (),
    timeframe: pb.Timeframe.ValueType = pb.TIMEFRAME_UNSPECIFIED,
) -> pb.StreamRequest:
    return pb.StreamRequest(
        subscribe=pb.Subscribe(channel=channel, symbols=symbols, timeframe=timeframe)
    )


def order_request(client_order_id: str = "co-1") -> pb.OrderRequest:
    return pb.OrderRequest(
        client_order_id=client_order_id,
        symbol=WINZ25,
        side=pb.ORDER_SIDE_BUY,
        type=pb.ORDER_TYPE_MARKET,
        volume="1",
        time_in_force=pb.TIME_IN_FORCE_DAY,
    )


def websocket_message(app: Any, *requests: dict[str, Any]) -> dict[str, Any]:
    """What /v1/stream answers to ``requests``: the last message of one per request."""
    message: dict[str, Any] = {}
    with TypedTestClient(app) as http:
        with http.websocket_connect("/v1/stream") as ws:
            for request in requests:
                ws.send_json(request)
                message = ws.receive_json()
    return message


async def test_ping_receives_the_pong_the_websocket_sends() -> None:
    app, _fake, state = build_test_app()
    wire = websocket_message(app, {"op": "ping"})
    async with serve_grpc(state) as channel:
        call = pb_grpc.StreamingStub(channel).Stream()
        await call.write(PING)
        event = await read_one(call)
        call.cancel()

    assert_same_ocsf_event(event, wire)
    assert event.metadata.event_code == "pong"
    assert event.class_uid == ocsf.API_ACTIVITY.uid
    assert event.status_id == ocsf.Status.SUCCESS
    assert event.api.operation == "ping"
    assert event.actor.user.uid == state.expected_account_id
    assert event.src_endpoint.ip == "127.0.0.1"
    assert event.src_endpoint.port > 0
    assert event.WhichOneof("payload") is None


async def test_the_ticks_channel_delivers_the_event_the_websocket_sends() -> None:
    app, fake, state = build_test_app()
    fake.set_tick(
        WINZ25, bid=Decimal("100.5"), ask=Decimal(101), last=Decimal(100), volume=Decimal(3)
    )
    wire = websocket_message(app, {"op": "subscribe", "channel": "ticks", "symbols": [WINZ25]})
    async with serve_grpc(state) as channel:
        call = pb_grpc.StreamingStub(channel).Stream()
        await call.write(subscribe(pb.CHANNEL_TICKS, symbols=(WINZ25,)))
        event = await read_one(call)
        call.cancel()

    assert_same_ocsf_event(event, wire)
    assert event.class_uid == ocsf.BASE_EVENT.uid
    assert (event.metadata.event_code, event.metadata.log_name) == ("ticks", f"ticks:{WINZ25}")
    assert event.metadata.sequence == 1
    assert event.time == state.clock.now_ms()
    assert event.metadata.original_time == str(state.clock.now_ms())
    assert event.source_time_unit == pb.SOURCE_TIME_UNIT_MILLISECONDS
    assert event.time_basis == pb.TIME_BASIS_EXCHANGE_REPORTED
    assert event.WhichOneof("payload") == "tick"
    payload = wire["unmapped"]["payload"]
    assert (event.tick.symbol, event.tick.bid, event.tick.ask, event.tick.last) == (
        payload["symbol"],
        payload["bid"],
        payload["ask"],
        payload["last"],
    )
    assert (event.tick.bid, event.tick.volume) == ("100.5", "3")


async def test_the_bars_channel_only_ever_delivers_closed_bars() -> None:
    state, fake = build_test_app_state()
    fake.push_bar(WINZ25, Timeframe.D1, time=1, o=1, h=2, low=1, c=1, volume=10)
    async with serve_grpc(state) as channel:
        call = pb_grpc.StreamingStub(channel).Stream()
        await call.write(subscribe(pb.CHANNEL_BARS, symbols=(WINZ25,), timeframe=pb.TIMEFRAME_D1))
        fake.push_bar(WINZ25, Timeframe.D1, time=2, o=1, h=2, low=1, c=2, volume=10)
        event = await read_one(call)
        call.cancel()

    assert event.metadata.event_code == "bars"
    assert event.metadata.log_name == f"bars:{WINZ25}:D1"
    assert event.source_time_unit == pb.SOURCE_TIME_UNIT_SECONDS
    assert (event.bar.timeframe, event.bar.close) == (pb.TIMEFRAME_D1, "1")


async def test_the_book_channel_delivers_a_snapshot_and_does_not_repeat_it() -> None:
    state, fake = build_test_app_state()
    fake.set_book(WINZ25, (BookLevel(side=BookSide.BUY, price=Decimal(100), volume=Decimal(10)),))
    async with serve_grpc(state) as channel:
        call = pb_grpc.StreamingStub(channel).Stream()
        await call.write(subscribe(pb.CHANNEL_BOOK, symbols=(WINZ25,)))
        first = await read_one(call)
        await call.write(PING)
        second = await read_one(call)
        call.cancel()

    assert [(level.side, level.price, level.volume) for level in first.book.levels] == [
        (pb.BOOK_SIDE_BUY, "100", "10")
    ]
    assert first.time_basis == pb.TIME_BASIS_BRIDGE_OBSERVED
    assert second.metadata.event_code == "pong"


async def test_unsubscribing_stops_a_channel() -> None:
    state, fake = build_test_app_state()
    async with serve_grpc(state) as channel:
        call = pb_grpc.StreamingStub(channel).Stream()
        await call.write(subscribe(pb.CHANNEL_TICKS, symbols=(WINZ25,)))
        fake.set_tick(WINZ25, bid=Decimal(1), ask=Decimal(2), last=Decimal(1), volume=Decimal(1))
        first = await read_one(call)
        await call.write(
            pb.StreamRequest(
                unsubscribe=pb.Unsubscribe(channel=pb.CHANNEL_TICKS, symbols=(WINZ25,))
            )
        )
        await call.write(PING)
        # The pong answers the ping sent after the unsubscribe: once it is read,
        # the unsubscribe has been applied and a new quote must not be published.
        pong = await read_one(call)
        fake.set_tick(WINZ25, bid=Decimal(5), ask=Decimal(6), last=Decimal(5), volume=Decimal(1))
        await call.write(PING)
        after = await read_one(call)
        call.cancel()

    assert first.metadata.event_code == "ticks"
    assert pong.metadata.event_code == "pong"
    assert after.metadata.event_code == "pong"


async def test_a_client_that_stops_sending_keeps_receiving() -> None:
    clock = FixedClock(NOW_MS)
    state, fake = build_test_app_state(clock=clock)
    async with serve_grpc(state) as channel:
        call = pb_grpc.StreamingStub(channel).Stream()
        await call.write(subscribe(pb.CHANNEL_TICKS, symbols=(WINZ25,)))
        await call.done_writing()
        fake.set_tick(WINZ25, bid=Decimal(1), ask=Decimal(2), last=Decimal(1), volume=Decimal(1))
        first = await read_one(call)
        clock.advance(1)  # a quote with a new time; the poller skips one it already sent
        fake.set_tick(WINZ25, bid=Decimal(3), ask=Decimal(4), last=Decimal(3), volume=Decimal(1))
        second = await read_one(call)
        call.cancel()

    assert (first.metadata.sequence, second.metadata.sequence) == (1, 2)
    assert second.tick.bid == "3"


async def test_executions_deliver_the_entity_event_the_websocket_sends() -> None:
    app, _fake, state = build_test_app()
    async with serve_grpc(state) as channel:
        call = pb_grpc.StreamingStub(channel).Stream()
        await call.write(subscribe(pb.CHANNEL_EXECUTIONS))
        await pb_grpc.TradingStub(channel).SubmitOrder(order_request())
        event = await read_one(call)
        call.cancel()
    wire = websocket_message(app, {"op": "subscribe", "channel": "executions"})

    assert_same_ocsf_event(event, wire)
    assert event.class_uid == ocsf.ENTITY_MANAGEMENT.uid
    assert (event.metadata.log_name, event.metadata.sequence) == ("executions", 1)
    assert event.metadata.uid == "executions:1"
    assert event.metadata.correlation_uid == "co-1"
    data = wire["entity"]["data"]
    assert event.execution.client_order_id == data["client_order_id"] == "co-1"
    assert event.execution.symbol == data["symbol"]
    assert pb.OrderState.Name(event.execution.state) == "ORDER_STATE_" + data["state"]
    assert event.execution.HasField("ticket") is (data["ticket"] is not None)


async def test_resume_starts_after_the_events_the_client_already_has() -> None:
    state, _fake = build_test_app_state()
    async with serve_grpc(state) as channel:
        trading = pb_grpc.TradingStub(channel)
        for client_order_id in ("co-1", "co-2"):
            await trading.SubmitOrder(order_request(client_order_id))
        last_seq = (
            state.sequence_log.read_from_no_transaction(stream="executions", from_seq=1)
            .events[-1]
            .seq
        )
        call = pb_grpc.StreamingStub(channel).Stream()
        await call.write(pb.StreamRequest(resume=pb.Resume(from_seq=last_seq)))
        event = await read_one(call)
        call.cancel()

    assert last_seq > 1
    assert event.metadata.sequence == last_seq
    assert event.execution.client_order_id == "co-2"


async def test_resume_with_an_expired_cursor_returns_the_websockets_resync_required() -> None:
    app, _fake, state = build_test_app()
    async with serve_grpc(state) as channel:
        await pb_grpc.TradingStub(channel).SubmitOrder(order_request())
        state.sequence_log.prune_no_transaction(stream="executions", keep_last=0)
        call = pb_grpc.StreamingStub(channel).Stream()
        await call.write(pb.StreamRequest(resume=pb.Resume(from_seq=1)))
        event = await read_one(call)
        call.cancel()
    wire = websocket_message(app, {"op": "resume", "channel": "executions", "from_seq": 1})

    assert_same_ocsf_event(event, wire)
    assert event.metadata.event_code == "RESYNC_REQUIRED"
    assert event.metadata.log_name == "executions"
    assert (event.status_id, event.status_code) == (ocsf.Status.FAILURE, "RESYNC_REQUIRED")
    assert event.api.operation == "resume"
    assert event.WhichOneof("payload") is None


async def test_the_account_and_status_channels_deliver_a_snapshot_on_subscribe() -> None:
    app, _fake, state = build_test_app()
    status_wire = websocket_message(app, {"op": "subscribe", "channel": "status"})
    account_wire = websocket_message(app, {"op": "subscribe", "channel": "account"})
    async with serve_grpc(state) as channel:
        call = pb_grpc.StreamingStub(channel).Stream()
        await call.write(subscribe(pb.CHANNEL_STATUS))
        status = await read_one(call)
        await call.write(subscribe(pb.CHANNEL_ACCOUNT))
        account = await read_one(call)
        call.cancel()

    assert_same_ocsf_event(status, status_wire)
    assert status.class_uid == ocsf.APPLICATION_LIFECYCLE.uid
    assert status.status.kill_switch_state == pb.KILL_SWITCH_STATE_NORMAL
    assert (
        status.status.terminal_connected is status_wire["unmapped"]["payload"]["terminal_connected"]
    )
    assert not status.status.HasField("symbol")
    assert_same_ocsf_event(account, account_wire)
    assert account.account.balance == account_wire["unmapped"]["payload"]["balance"]


async def test_a_status_message_about_a_symbol_carries_its_trading_phase() -> None:
    state, _fake = build_test_app_state()
    async with serve_grpc(state) as channel:
        call = pb_grpc.StreamingStub(channel).Stream()
        await call.write(subscribe(pb.CHANNEL_BOOK, symbols=(WINZ25,)))
        await call.write(subscribe(pb.CHANNEL_STATUS))
        first = await read_one(call)
        second = await read_one(call)
        call.cancel()

    assert not first.status.HasField("symbol")
    assert second.status.symbol == WINZ25
    assert second.status.trading_phase != pb.TRADING_PHASE_UNSPECIFIED


@pytest.mark.parametrize(
    ("request_message", "field"),
    [
        (subscribe(pb.CHANNEL_BARS, symbols=(WINZ25,)), "timeframe"),
        (subscribe(pb.CHANNEL_UNSPECIFIED), "channel"),
        (pb.StreamRequest(resume=pb.Resume(from_seq=0)), "from_seq"),
        (pb.StreamRequest(), "op"),
    ],
    ids=["bars-without-timeframe", "no-channel", "resume-from-zero", "no-operation"],
)
async def test_an_invalid_client_message_ends_the_stream_with_the_validation_problem(
    request_message: pb.StreamRequest, field: str
) -> None:
    state, _fake = build_test_app_state()
    async with serve_grpc(state) as channel:
        call = pb_grpc.StreamingStub(channel).Stream()
        await call.write(request_message)
        with pytest.raises(grpc.aio.AioRpcError) as failed:
            await read_one(call)

    problem = problem_of(failed.value)
    assert failed.value.code() is grpc.StatusCode.INVALID_ARGUMENT
    assert problem.code == "ValidationError"
    assert problem.instance == "/mt5_bridge.v1.Streaming/Stream"
    assert [error.field for error in problem.errors] == [field]
