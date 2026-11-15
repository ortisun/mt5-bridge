"""API telemetry (plan §84): a server span per request that continues the
caller's W3C trace, child spans down to the terminal call, one span per
WebSocket connection, and the HTTP server metrics."""

from decimal import Decimal
from pathlib import Path

import pytest
from opentelemetry.metrics import NoOpMeter
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.trace import SpanKind, StatusCode

from mt5_bridge.api.middleware import Message, Receive, Scope, Send
from mt5_bridge.api.telemetry import TelemetryMiddleware
from mt5_bridge.core.worker import CALL_SPAN, DISPATCH_SPAN
from mt5_bridge.main import boot
from mt5_bridge.secrets import InMemorySecretReader

from testkit.app_builder import build_test_app
from testkit.fixtures import WINZ25
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


def submit_body(client_order_id: str = "co-1") -> dict[str, object]:
    return {
        "client_order_id": client_order_id,
        "symbol": WINZ25,
        "side": "BUY",
        "type": "MARKET",
        "volume": "1",
        "time_in_force": "DAY",
    }


def test_a_request_with_a_traceparent_continues_the_callers_trace(
    spans: InMemorySpanExporter,
) -> None:
    app, _fake_client, _state = build_test_app()

    with TypedTestClient(app) as http:
        response = http.get(
            "/v1/account",
            headers={"traceparent": TRACEPARENT, "tracestate": "vendor=1"},
        )

    assert response.status_code == 200
    server = span_named(spans.get_finished_spans(), "GET /v1/account")
    assert server.kind is SpanKind.SERVER
    assert trace_id(server) == TRACE_ID
    assert parent_id(server) == PARENT_SPAN_ID
    assert server.parent is not None and server.parent.is_remote
    assert server.parent.trace_state.get("vendor") == "1"
    assert server.attributes is not None
    assert server.attributes["http.request.method"] == "GET"
    assert server.attributes["http.route"] == "/v1/account"
    assert server.attributes["url.path"] == "/v1/account"
    assert server.attributes["http.response.status_code"] == 200
    assert server.status.status_code is StatusCode.UNSET


def test_a_request_without_a_traceparent_starts_a_new_trace(spans: InMemorySpanExporter) -> None:
    app, _fake_client, _state = build_test_app()

    with TypedTestClient(app) as http:
        http.get("/v1/account")

    assert span_named(spans.get_finished_spans(), "GET /v1/account").parent is None


def test_the_span_is_named_by_the_route_template_and_holds_no_header_or_query(
    spans: InMemorySpanExporter,
) -> None:
    app, _fake_client, _state = build_test_app()

    with TypedTestClient(app) as http:
        response = http.get("/v1/orders/co-missing?x=1", headers={"x-private": "hdr-value"})

    assert response.status_code == 404
    server = span_named(spans.get_finished_spans(), "GET /v1/orders/{client_order_id}")
    # A client error is not a server-span error (OTel HTTP conventions).
    assert server.status.status_code is StatusCode.UNSET
    assert server.attributes is not None
    recorded = " ".join(f"{key}={value}" for key, value in server.attributes.items())
    assert "hdr-value" not in recorded
    assert "x=1" not in recorded
    assert "url.query" not in server.attributes


def test_health_and_metrics_requests_are_not_traced(spans: InMemorySpanExporter) -> None:
    app, _fake_client, _state = build_test_app()

    with TypedTestClient(app) as http:
        assert http.get("/v1/health").status_code == 200
        assert http.get("/v1/metrics").status_code == 200

    # Not even the worker hop behind /v1/health: it has no trace to join.
    assert spans.get_finished_spans() == ()


def test_an_order_submission_is_traced_down_to_the_terminal_call(
    spans: InMemorySpanExporter,
) -> None:
    app, fake_client, _state = build_test_app()
    fake_client.set_tick(
        WINZ25, bid=Decimal(100), ask=Decimal(101), last=Decimal(100), volume=Decimal(1)
    )

    with TypedTestClient(app) as http:
        response = http.post(
            "/v1/orders",
            json=submit_body(),
            headers={"traceparent": TRACEPARENT},
        )

    assert response.status_code == 201
    finished = spans.get_finished_spans()
    assert {trace_id(item) for item in finished} == {TRACE_ID}
    server = span_named(finished, "POST /v1/orders")
    submit = span_named(finished, "mt5_bridge.order.submit")
    assert parent_id(submit) == span_id(server)
    assert submit.attributes is not None
    assert submit.attributes["mt5_bridge.order.client_order_id"] == "co-1"
    assert submit.attributes["mt5_bridge.order.symbol"] == WINZ25
    assert submit.attributes["mt5_bridge.order.side"] == "BUY"
    assert submit.attributes["mt5_bridge.order.state"] == "FILLED"
    assert "mt5_bridge.order.finalized" in [event.name for event in submit.events]
    by_id = {span_id(item): item for item in finished}
    calls = [item for item in finished if item.name == CALL_SPAN]
    assert calls
    for call in calls:
        dispatch = by_id[parent_id(call) or 0]
        assert dispatch.name == DISPATCH_SPAN
    # The order's own terminal calls hang under the submit span.
    assert any(
        parent_id(item) == span_id(submit) for item in finished if item.name == DISPATCH_SPAN
    )


def test_boot_reconciliation_has_its_own_span(spans: InMemorySpanExporter, tmp_path: Path) -> None:
    result = boot(
        {"MT5_BRIDGE_CLIENT_BACKEND": "fake", "MT5_BRIDGE_DB_PATH": str(tmp_path / "bridge.db")},
        InMemorySecretReader(),
    )
    result.state.worker.stop()

    reconciliation = span_named(spans.get_finished_spans(), "mt5_bridge.reconciliation.boot")
    assert reconciliation.attributes is not None
    assert reconciliation.attributes["mt5_bridge.reconciliation.pending"] == 0


def test_the_http_server_metrics_are_served_by_the_prometheus_endpoint() -> None:
    app, _fake_client, _state = build_test_app()

    with TypedTestClient(app) as http:
        http.get("/v1/account")
        body = http.get("/v1/metrics").text

    duration = next(
        line
        for line in body.splitlines()
        if line.startswith("http_server_request_duration_seconds_count")
        and 'http_route="/v1/account"' in line
    )
    assert 'http_request_method="GET"' in duration
    assert 'http_response_status_code="200"' in duration
    assert duration.endswith(" 1.0")
    assert 'le="0.005"' in body
    # The scrape itself is the one request in flight.
    assert any(
        line.startswith("http_server_active_requests{") and line.endswith(" 1.0")
        for line in body.splitlines()
    )
    # The existing metric names are untouched.
    assert "mt5_bridge_kill_switch_state" in body


def test_a_websocket_connection_is_one_span_however_many_messages_it_carries(
    spans: InMemorySpanExporter,
) -> None:
    app, fake_client, _state = build_test_app()
    fake_client.set_tick(
        WINZ25, bid=Decimal(100), ask=Decimal(101), last=Decimal(100), volume=Decimal(1)
    )

    with TypedTestClient(app) as http:
        with http.websocket_connect("/v1/stream", headers={"traceparent": TRACEPARENT}) as ws:
            ws.send_json({"op": "subscribe", "channel": "ticks", "symbols": [WINZ25]})
            assert ws.receive_json()["channel"] == "ticks"
            ws.send_json({"op": "ping"})
            assert ws.receive_json() == {"type": "pong"}

    finished = spans.get_finished_spans()
    # One span for the connection, one for the subscription; none per tick,
    # per ping or per market-data poll.
    assert sorted(item.name for item in finished) == ["GET /v1/stream", "mt5_bridge.ws.subscribe"]
    connection = span_named(finished, "GET /v1/stream")
    subscribe = span_named(finished, "mt5_bridge.ws.subscribe")
    assert connection.kind is SpanKind.SERVER
    assert parent_id(connection) == PARENT_SPAN_ID
    assert parent_id(subscribe) == span_id(connection)
    assert subscribe.attributes is not None
    assert subscribe.attributes["mt5_bridge.ws.channel"] == "ticks"
    assert subscribe.attributes["mt5_bridge.ws.symbol_count"] == 1
    assert connection.attributes is not None
    assert connection.attributes["network.protocol.name"] == "websocket"
    assert connection.attributes["mt5_bridge.ws.accepted"] is True
    assert connection.attributes["mt5_bridge.ws.messages_sent"] == 2
    assert connection.attributes["mt5_bridge.ws.messages_received"] == 2


def http_scope(path: str = "/boom") -> Scope:
    return {"type": "http", "method": "GET", "path": path, "scheme": "http", "headers": []}


async def _receive() -> Message:
    return {"type": "http.request", "body": b"", "more_body": False}


async def _discard(message: Message) -> None:
    del message


@pytest.mark.asyncio
async def test_an_escaping_exception_sets_the_error_status_on_the_server_span(
    spans: InMemorySpanExporter,
) -> None:
    async def failing_app(scope: Scope, receive: Receive, send: Send) -> None:
        del scope, receive, send
        raise RuntimeError("terminal exploded")

    middleware = TelemetryMiddleware(failing_app, meter=NoOpMeter("test"))

    with pytest.raises(RuntimeError):
        await middleware(http_scope(), _receive, _discard)

    server = span_named(spans.get_finished_spans(), "GET")
    assert server.status.status_code is StatusCode.ERROR
    assert server.attributes is not None
    assert server.attributes["error.type"] == "RuntimeError"
    assert server.attributes["http.response.status_code"] == 500
    assert [event.name for event in server.events] == ["exception"]


@pytest.mark.asyncio
async def test_a_5xx_answer_sets_the_error_status_and_a_4xx_does_not(
    spans: InMemorySpanExporter,
) -> None:
    def answering(status: int):
        async def app(scope: Scope, receive: Receive, send: Send) -> None:
            del scope, receive
            await send({"type": "http.response.start", "status": status, "headers": []})
            await send({"type": "http.response.body", "body": b""})

        return app

    for status in (503, 404):
        middleware = TelemetryMiddleware(answering(status), meter=NoOpMeter("test"))
        await middleware(http_scope(f"/status/{status}"), _receive, _discard)

    by_status = {
        (item.attributes or {})["http.response.status_code"]: item
        for item in spans.get_finished_spans()
    }
    assert by_status[503].status.status_code is StatusCode.ERROR
    assert (by_status[503].attributes or {})["error.type"] == "503"
    assert by_status[404].status.status_code is StatusCode.UNSET
