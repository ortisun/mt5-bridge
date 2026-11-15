"""gRPC telemetry (plan §84): a server span per call that continues the
caller's W3C trace, one span per stream, and the RPC server metrics."""

from decimal import Decimal

import grpc
import pytest
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.trace import SpanKind, StatusCode

from mt5_bridge.api.grpc_api.generated import mt5_bridge_pb2 as pb
from mt5_bridge.api.grpc_api.generated import mt5_bridge_pb2_grpc as pb_grpc
from mt5_bridge.core.worker import CALL_SPAN, DISPATCH_SPAN
from mt5_bridge.mt5.fake_client import FakeMt5Client

from testkit.app_builder import build_test_app, build_test_app_state
from testkit.fixtures import WINZ25
from testkit.grpc_client import read_one, serve_grpc
from testkit.http_client import TypedTestClient
from testkit.telemetry import (
    PARENT_SPAN_ID,
    TRACE_ID,
    TRACEPARENT,
    parent_id,
    span_id,
    span_named,
    trace_id,
)

pytestmark = pytest.mark.asyncio

TRACE_METADATA = (("traceparent", TRACEPARENT), ("tracestate", "vendor=1"))
GET_ACCOUNT = "mt5_bridge.v1.MarketData/GetAccount"
SUBMIT_ORDER = "mt5_bridge.v1.Trading/SubmitOrder"
STREAM = "mt5_bridge.v1.Streaming/Stream"


def order_request() -> pb.OrderRequest:
    return pb.OrderRequest(
        client_order_id="co-1",
        symbol=WINZ25,
        side=pb.ORDER_SIDE_BUY,
        type=pb.ORDER_TYPE_MARKET,
        volume="1",
        time_in_force=pb.TIME_IN_FORCE_DAY,
    )


async def test_a_call_with_a_traceparent_continues_the_callers_trace(
    spans: InMemorySpanExporter,
) -> None:
    state, _fake = build_test_app_state()
    async with serve_grpc(state) as channel:
        await pb_grpc.MarketDataStub(channel).GetAccount(
            pb.GetAccountRequest(), metadata=TRACE_METADATA
        )

    server = span_named(spans.get_finished_spans(), GET_ACCOUNT)
    assert server.kind is SpanKind.SERVER
    assert trace_id(server) == TRACE_ID
    assert parent_id(server) == PARENT_SPAN_ID
    assert server.parent is not None and server.parent.is_remote
    assert server.attributes is not None
    assert server.attributes["rpc.system.name"] == "grpc"
    assert server.attributes["rpc.method"] == GET_ACCOUNT
    assert server.attributes["rpc.response.status_code"] == "OK"
    assert server.attributes["server.address"] == "127.0.0.1"
    assert isinstance(server.attributes["server.port"], int)
    assert server.status.status_code is StatusCode.UNSET
    # Nothing of the metadata but the user agent reaches the span.
    assert not any("traceparent" in str(value) for value in server.attributes.values())
    assert str(server.attributes["user_agent.original"]).startswith("grpc-python")


async def test_a_call_without_a_traceparent_starts_a_new_trace(
    spans: InMemorySpanExporter,
) -> None:
    state, _fake = build_test_app_state()
    async with serve_grpc(state) as channel:
        await pb_grpc.MarketDataStub(channel).GetAccount(pb.GetAccountRequest())

    server = span_named(spans.get_finished_spans(), GET_ACCOUNT)
    assert server.parent is None
    assert trace_id(server) != TRACE_ID


async def test_health_calls_are_not_traced(spans: InMemorySpanExporter) -> None:
    state, _fake = build_test_app_state()
    async with serve_grpc(state) as channel:
        await pb_grpc.MarketDataStub(channel).GetHealth(pb.GetHealthRequest())

    assert spans.get_finished_spans() == ()


async def test_an_order_submission_is_traced_down_to_the_terminal_call(
    spans: InMemorySpanExporter,
) -> None:
    state, fake = build_test_app_state()
    fake.set_tick(WINZ25, bid=Decimal(100), ask=Decimal(101), last=Decimal(100), volume=Decimal(1))
    async with serve_grpc(state) as channel:
        await pb_grpc.TradingStub(channel).SubmitOrder(order_request(), metadata=TRACE_METADATA)

    finished = spans.get_finished_spans()
    server = span_named(finished, SUBMIT_ORDER)
    submit = span_named(finished, "mt5_bridge.order.submit")
    assert parent_id(submit) == span_id(server)
    assert {trace_id(item) for item in finished} == {TRACE_ID}
    names = {item.name for item in finished}
    assert {DISPATCH_SPAN, CALL_SPAN} <= names


async def test_a_client_fault_is_not_an_error_and_a_server_fault_is(
    spans: InMemorySpanExporter,
) -> None:
    state, fake = build_test_app_state()
    async with serve_grpc(state) as channel:
        stub = pb_grpc.MarketDataStub(channel)
        with pytest.raises(grpc.aio.AioRpcError):
            await stub.GetSymbol(pb.GetSymbolRequest(symbol="NOPE"))
        fake.simulate_disconnect()
        with pytest.raises(grpc.aio.AioRpcError):
            await stub.GetAccount(pb.GetAccountRequest())

    finished = spans.get_finished_spans()
    not_found = span_named(finished, "mt5_bridge.v1.MarketData/GetSymbol")
    unavailable = span_named(finished, GET_ACCOUNT)
    assert not_found.attributes is not None and unavailable.attributes is not None
    assert not_found.attributes["rpc.response.status_code"] == "NOT_FOUND"
    assert not_found.status.status_code is StatusCode.UNSET
    assert "error.type" not in not_found.attributes
    assert unavailable.attributes["rpc.response.status_code"] == "UNAVAILABLE"
    assert unavailable.attributes["error.type"] == "UNAVAILABLE"
    assert unavailable.status.status_code is StatusCode.ERROR


async def test_an_unhandled_exception_is_recorded_on_the_server_span(
    spans: InMemorySpanExporter, monkeypatch: pytest.MonkeyPatch
) -> None:
    state, _fake = build_test_app_state()

    def fail(self: FakeMt5Client) -> None:
        raise RuntimeError("boom")

    monkeypatch.setattr(FakeMt5Client, "terminal_info", fail)
    async with serve_grpc(state) as channel:
        with pytest.raises(grpc.aio.AioRpcError):
            await pb_grpc.MarketDataStub(channel).GetTerminal(pb.GetTerminalRequest())

    server = span_named(spans.get_finished_spans(), "mt5_bridge.v1.MarketData/GetTerminal")
    assert server.attributes is not None
    assert server.attributes["rpc.response.status_code"] == "INTERNAL"
    assert server.attributes["error.type"] == "RuntimeError"
    assert server.status.status_code is StatusCode.ERROR
    assert [event.name for event in server.events] == ["exception"]


async def test_a_stream_is_one_span_however_many_messages_it_carries(
    spans: InMemorySpanExporter,
) -> None:
    state, fake = build_test_app_state()
    fake.set_tick(WINZ25, bid=Decimal(100), ask=Decimal(101), last=Decimal(100), volume=Decimal(1))
    async with serve_grpc(state) as channel:
        call = pb_grpc.StreamingStub(channel).Stream(metadata=TRACE_METADATA)
        await call.write(
            pb.StreamRequest(subscribe=pb.Subscribe(channel=pb.CHANNEL_TICKS, symbols=(WINZ25,)))
        )
        assert (await read_one(call)).metadata.event_code == "ticks"
        await call.write(pb.StreamRequest(ping=pb.Ping()))
        assert (await read_one(call)).metadata.event_code == "pong"
        call.cancel()

    finished = spans.get_finished_spans()
    # One span for the call, one for the subscription; none per tick, per
    # ping or per market-data poll.
    assert sorted(item.name for item in finished) == ["mt5_bridge.grpc.subscribe", STREAM]
    stream = span_named(finished, STREAM)
    subscription = span_named(finished, "mt5_bridge.grpc.subscribe")
    assert stream.kind is SpanKind.SERVER
    assert parent_id(stream) == PARENT_SPAN_ID
    assert parent_id(subscription) == span_id(stream)
    assert subscription.attributes is not None
    assert subscription.attributes["mt5_bridge.grpc.channel"] == "ticks"
    assert subscription.attributes["mt5_bridge.grpc.symbol_count"] == 1
    assert stream.attributes is not None
    assert stream.attributes["rpc.response.status_code"] == "CANCELLED"
    assert stream.attributes["mt5_bridge.grpc.messages_sent"] == 2
    assert stream.attributes["mt5_bridge.grpc.messages_received"] == 2
    assert stream.status.status_code is StatusCode.UNSET


async def test_the_rpc_server_metrics_are_served_by_the_prometheus_endpoint() -> None:
    app, _fake, state = build_test_app()
    async with serve_grpc(state) as channel:
        stub = pb_grpc.MarketDataStub(channel)
        await stub.GetAccount(pb.GetAccountRequest())
        await stub.GetHealth(pb.GetHealthRequest())
        with pytest.raises(grpc.aio.AioRpcError):
            await stub.GetSymbol(pb.GetSymbolRequest(symbol="NOPE"))
        call = pb_grpc.StreamingStub(channel).Stream()
        await call.write(pb.StreamRequest(ping=pb.Ping()))
        await read_one(call)
        with TypedTestClient(app) as http:
            body = http.get("/v1/metrics").text
        call.cancel()

    counts = [
        line
        for line in body.splitlines()
        if line.startswith("rpc_server_call_duration_seconds_count")
    ]
    account = next(line for line in counts if f'rpc_method="{GET_ACCOUNT}"' in line)
    assert 'rpc_system_name="grpc"' in account
    assert 'rpc_response_status_code="OK"' in account
    assert account.endswith(" 1.0")
    # An untraced call is still measured, and a failed one has its status.
    assert any('rpc_method="mt5_bridge.v1.MarketData/GetHealth"' in line for line in counts)
    assert any('rpc_response_status_code="NOT_FOUND"' in line for line in counts)
    # The stream open during the scrape is the one counted.
    assert any(
        line.startswith("mt5_bridge_grpc_active_streams{") and line.endswith(" 1.0")
        for line in body.splitlines()
    )
