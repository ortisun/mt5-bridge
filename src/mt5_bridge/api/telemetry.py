"""Server-side telemetry for the API: one span per HTTP request, one span per
WebSocket connection, and the HTTP server metrics (plan §84).

``TelemetryMiddleware`` is plain ASGI, like ``api.middleware``, and is added
last so it is the outermost application middleware: it also sees a request
that ``MaxBodySizeMiddleware`` rejects. An in-house middleware rather than
``opentelemetry-instrumentation-asgi``/``-fastapi`` (docs/observability.md
has the full reasoning): the instrumentation packages are pre-release, bring
four more dependencies, patch the framework, and open a span for every
WebSocket ``receive``/``send`` -- that is one span per market-data message,
which this bridge must not produce.

HTTP (OTel HTTP semantic conventions): the span continues the caller's trace
when the request carries a W3C ``traceparent`` (and ``tracestate``) header,
is named ``{method} {route}`` once routing has matched, and is marked failed
on a 5xx answer or an escaping exception. ``/v1/health`` and ``/v1/metrics``
get no span -- they are polled and scraped, and their worker hop then stays
untraced too -- but still count in the metrics. No request header other than
``User-Agent``, no query string, no body and no client address is ever copied
to a span.

WebSocket: one SERVER span for the whole connection, ended at disconnect. It
is deliberately *not* the current span while the connection loop runs, so the
market-data polls the loop submits every few milliseconds stay untraced. A
client ``subscribe``/``unsubscribe``/``resume`` gets a short child span
(``stream_operation_span``); published messages are only counted.
"""

from __future__ import annotations

from collections.abc import Generator, Mapping
from contextlib import contextmanager
from contextvars import ContextVar
from time import perf_counter
from typing import Any, cast

from opentelemetry import trace
from opentelemetry.context import Context
from opentelemetry.metrics import Meter
from opentelemetry.trace import Span, SpanKind, Status, StatusCode
from opentelemetry.trace.propagation.tracecontext import TraceContextTextMapPropagator

from mt5_bridge.api.middleware import ASGIApp, Message, Receive, Scope, Send
from mt5_bridge.core.tracing import (
    AttributeValue,
    error_type,
    record_error,
    span,
    start_span,
)

UNTRACED_PATHS = frozenset({"/v1/health", "/v1/metrics"})

HTTP_DURATION_METRIC = "http.server.request.duration"
HTTP_ACTIVE_REQUESTS_METRIC = "http.server.active_requests"
WS_ACTIVE_CONNECTIONS_METRIC = "mt5_bridge.ws.active_connections"

# OTel HTTP semantic conventions: the advised boundaries for a duration in seconds.
_DURATION_BUCKETS = (0.005, 0.01, 0.025, 0.05, 0.075, 0.1, 0.25, 0.5, 0.75, 1, 2.5, 5, 7.5, 10)
_KNOWN_METHODS = frozenset(
    {"CONNECT", "DELETE", "GET", "HEAD", "OPTIONS", "PATCH", "POST", "PUT", "TRACE"}
)
_TRACE_HEADERS = (b"traceparent", b"tracestate")
_STREAM_OPERATIONS = frozenset({"subscribe", "unsubscribe", "resume"})
_USER_AGENT_MAX_LENGTH = 256
_INTERNAL_ERROR_CLOSE_CODE = 1011

_PROPAGATOR = TraceContextTextMapPropagator()
_connection_span: ContextVar[Span | None] = ContextVar("mt5_bridge_ws_span", default=None)


def trace_context(carrier: Mapping[str, str]) -> Context:
    """The caller's W3C Trace Context from ``traceparent`` and ``tracestate``,
    or an empty context (a new trace). The gRPC server reads the same two
    entries from the call's metadata (``api.grpc_api.telemetry``)."""
    return _PROPAGATOR.extract(carrier, context=Context())


def _extract_trace_context(scope: Scope) -> Context:
    carrier: dict[str, str] = {}
    for raw_name, raw_value in scope.get("headers", ()):
        name = raw_name.lower()
        if name in _TRACE_HEADERS:
            key, value = name.decode("ascii"), raw_value.decode("latin-1")
            carrier[key] = f"{carrier[key]},{value}" if key in carrier else value
    return trace_context(carrier)


def _header(scope: Scope, wanted: bytes) -> str | None:
    for raw_name, raw_value in scope.get("headers", ()):
        if raw_name.lower() == wanted:
            return raw_value.decode("latin-1")
    return None


def _route(scope: Scope) -> str | None:
    """The matched route's template (low cardinality), never the raw path."""
    path = getattr(scope.get("route"), "path", None)
    return path if isinstance(path, str) and path else None


def _request_attributes(scope: Scope, method: str) -> dict[str, AttributeValue]:
    attributes: dict[str, AttributeValue] = {
        "http.request.method": method,
        "url.path": str(scope.get("path", "")),
        "url.scheme": str(scope.get("scheme", "http")),
    }
    http_version = scope.get("http_version")
    if http_version:
        attributes["network.protocol.version"] = str(http_version)
    server = scope.get("server")
    if server:
        attributes["server.address"] = str(server[0])
        if server[1] is not None:
            attributes["server.port"] = int(server[1])
    user_agent = _header(scope, b"user-agent")
    if user_agent:
        attributes["user_agent.original"] = user_agent[:_USER_AGENT_MAX_LENGTH]
    return attributes


class TelemetryMiddleware:
    def __init__(
        self, app: ASGIApp, *, meter: Meter, untraced_paths: frozenset[str] = UNTRACED_PATHS
    ) -> None:
        self._app = app
        self._untraced_paths = untraced_paths
        self._duration = meter.create_histogram(
            HTTP_DURATION_METRIC,
            unit="s",
            description="Duration of HTTP server requests",
            explicit_bucket_boundaries_advisory=_DURATION_BUCKETS,
        )
        self._active_requests = meter.create_up_down_counter(
            HTTP_ACTIVE_REQUESTS_METRIC,
            unit="{request}",
            description="Number of active HTTP server requests",
        )
        self._active_connections = meter.create_up_down_counter(
            WS_ACTIVE_CONNECTIONS_METRIC,
            unit="{connection}",
            description="Number of open WebSocket stream connections",
        )

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "http":
            await self._http(scope, receive, send)
        elif scope["type"] == "websocket":
            await self._websocket(scope, receive, send)
        else:
            await self._app(scope, receive, send)

    async def _http(self, scope: Scope, receive: Receive, send: Send) -> None:
        raw_method = str(scope.get("method", "")).upper()
        method = raw_method if raw_method in _KNOWN_METHODS else "_OTHER"
        metric_attributes: dict[str, AttributeValue] = {
            "http.request.method": method,
            "url.scheme": str(scope.get("scheme", "http")),
        }
        status: list[int] = []

        async def observing_send(message: Message) -> None:
            if message["type"] == "http.response.start":
                status.append(int(message["status"]))
            await send(message)

        self._active_requests.add(1, metric_attributes)
        started = perf_counter()
        failure: str | None = None
        try:
            if scope.get("path") in self._untraced_paths:
                await self._app(scope, receive, observing_send)
            else:
                await self._traced_http(scope, receive, observing_send, method, status)
        except Exception as error:
            failure = error_type(error)
            raise
        finally:
            self._active_requests.add(-1, metric_attributes)
            duration_attributes = dict(metric_attributes)
            route = _route(scope)
            if route is not None:
                duration_attributes["http.route"] = route
            # An exception that escapes is answered 500 by the server error
            # middleware outside this one.
            status_code = status[0] if status else (500 if failure else None)
            if status_code is not None:
                duration_attributes["http.response.status_code"] = status_code
                if failure is None and status_code >= 500:
                    failure = str(status_code)
            if failure is not None:
                duration_attributes["error.type"] = failure
            self._duration.record(perf_counter() - started, duration_attributes)

    async def _traced_http(
        self, scope: Scope, receive: Receive, send: Send, method: str, status: list[int]
    ) -> None:
        with span(
            method,
            kind=SpanKind.SERVER,
            attributes=_request_attributes(scope, method),
            context=_extract_trace_context(scope),
        ) as current:
            try:
                await self._app(scope, receive, send)
            except Exception:
                current.set_attribute("http.response.status_code", 500)
                raise
            finally:
                route = _route(scope)
                if route is not None:
                    current.update_name(f"{method} {route}")
                    current.set_attribute("http.route", route)
                if status:
                    current.set_attribute("http.response.status_code", status[0])
                    if status[0] >= 500:
                        current.set_attribute("error.type", str(status[0]))
                        current.set_status(Status(StatusCode.ERROR))

    async def _websocket(self, scope: Scope, receive: Receive, send: Send) -> None:
        path = str(scope.get("path", ""))
        attributes = _request_attributes(scope, "GET")
        attributes["network.protocol.name"] = "websocket"
        current = start_span(
            f"GET {path}",
            kind=SpanKind.SERVER,
            attributes=attributes,
            context=_extract_trace_context(scope),
        )
        counts = {"sent": 0, "received": 0}
        accepted = False

        async def observing_send(message: Message) -> None:
            nonlocal accepted
            kind = message["type"]
            if kind == "websocket.send":
                counts["sent"] += 1
            elif kind == "websocket.accept":
                accepted = True
                self._active_connections.add(1)
            elif kind == "websocket.close":
                _set_close_code(current, message.get("code"))
            await send(message)

        async def observing_receive() -> Message:
            message = await receive()
            kind = message["type"]
            if kind == "websocket.receive":
                counts["received"] += 1
            elif kind == "websocket.disconnect":
                _set_close_code(current, message.get("code"))
            return message

        restore = _connection_span.set(current)
        try:
            await self._app(scope, observing_receive, observing_send)
        except Exception as error:
            record_error(current, error)
            raise
        finally:
            _connection_span.reset(restore)
            if accepted:
                self._active_connections.add(-1)
            route = _route(scope)
            if route is not None:
                current.update_name(f"GET {route}")
                current.set_attribute("http.route", route)
            current.set_attribute("mt5_bridge.ws.accepted", accepted)
            current.set_attribute("mt5_bridge.ws.messages_sent", counts["sent"])
            current.set_attribute("mt5_bridge.ws.messages_received", counts["received"])
            current.end()


def _set_close_code(current: Span, code: object) -> None:
    if not isinstance(code, int):
        return
    current.set_attribute("mt5_bridge.ws.close_code", code)
    if code == _INTERNAL_ERROR_CLOSE_CODE:
        current.set_attribute("error.type", str(code))
        current.set_status(Status(StatusCode.ERROR))


@contextmanager
def stream_operation_span(
    message: Any, *, connection: Span | None = None, namespace: str = "mt5_bridge.ws"
) -> Generator[None]:
    """A child span of the connection span around one client stream operation
    (``subscribe``, ``unsubscribe``, ``resume``). Anything else -- a ``ping``,
    a malformed message, a connection with no span -- runs untraced.

    The WebSocket connection's span is found by itself; the gRPC stream
    passes its call span as ``connection`` and its own ``namespace``."""
    connection = connection or _connection_span.get()
    operation = _field(message, "op")
    if connection is None or operation not in _STREAM_OPERATIONS:
        yield
        return
    attributes: dict[str, AttributeValue] = {f"{namespace}.operation": operation}
    channel = _field(message, "channel")
    if channel is not None:
        attributes[f"{namespace}.channel"] = channel
    symbols: object = message.get("symbols")
    if isinstance(symbols, list):
        attributes[f"{namespace}.symbol_count"] = len(cast("list[object]", symbols))
    with span(
        f"{namespace}.{operation}",
        attributes=attributes,
        context=trace.set_span_in_context(connection, Context()),
    ):
        yield


def _field(message: Any, name: str) -> str | None:
    if not isinstance(message, dict):
        return None
    value = cast("dict[object, object]", message).get(name)
    return value if isinstance(value, str) else None


__all__ = [
    "HTTP_ACTIVE_REQUESTS_METRIC",
    "HTTP_DURATION_METRIC",
    "UNTRACED_PATHS",
    "WS_ACTIVE_CONNECTIONS_METRIC",
    "TelemetryMiddleware",
    "stream_operation_span",
    "trace_context",
]
