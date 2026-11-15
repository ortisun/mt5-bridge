"""Server-side telemetry for the gRPC API: one span and one duration
measurement per call (plan §84), the gRPC counterpart of ``api.telemetry``.

The span follows the OTel RPC semantic conventions: it is named after the
method (``mt5_bridge.v1.Trading/SubmitOrder``), has the kind SERVER, and
carries ``rpc.system.name``, ``rpc.method`` and ``rpc.response.status_code``.
It continues the caller's trace when the call's metadata has a W3C
``traceparent`` (and ``tracestate``) entry. A call is marked failed when it
ends with a status that is the server's fault (UNKNOWN, DEADLINE_EXCEEDED,
UNIMPLEMENTED, INTERNAL, UNAVAILABLE, DATA_LOSS); a status the client caused,
NOT_FOUND for one, is not an error. No metadata entry other than
``user-agent``, no message and no client address is copied to a span.

A unary call's span is the current span while its handler runs, so the order
and worker spans are its children. ``GetHealth`` gets no span, like
``/v1/health``: it is polled.

The ``Stream`` call is one span from its start to its end, however many
messages it carries. As with the WebSocket connection, it is not the current
span while the stream runs, so the market-data polls stay untraced; a client
``subscribe``/``unsubscribe``/``resume`` gets a short child span and the
messages are only counted.
"""

from __future__ import annotations

from time import perf_counter
from types import TracebackType
from typing import Any

import grpc
from opentelemetry import trace
from opentelemetry.metrics import Meter
from opentelemetry.trace import Span, SpanKind, Status, StatusCode

from mt5_bridge.api.telemetry import trace_context
from mt5_bridge.core.tracing import AttributeValue, record_error, start_span

RPC_SYSTEM_NAME = "grpc"
RPC_DURATION_METRIC = "rpc.server.call.duration"
GRPC_ACTIVE_STREAMS_METRIC = "mt5_bridge.grpc.active_streams"

UNTRACED_METHODS = frozenset({"mt5_bridge.v1.MarketData/GetHealth"})

# OTel RPC semantic conventions: the advised boundaries for a duration in seconds.
_DURATION_BUCKETS = (0.005, 0.01, 0.025, 0.05, 0.075, 0.1, 0.25, 0.5, 0.75, 1, 2.5, 5, 7.5, 10)
# OTel gRPC conventions: the statuses that are an error on the server side.
_SERVER_ERROR_STATUSES = frozenset(
    {
        grpc.StatusCode.UNKNOWN,
        grpc.StatusCode.DEADLINE_EXCEEDED,
        grpc.StatusCode.UNIMPLEMENTED,
        grpc.StatusCode.INTERNAL,
        grpc.StatusCode.UNAVAILABLE,
        grpc.StatusCode.DATA_LOSS,
    }
)
_TRACE_ENTRIES = ("traceparent", "tracestate")
_USER_AGENT_MAX_LENGTH = 256

type Context = grpc.aio.ServicerContext[Any, Any]


def _metadata(context: Context) -> dict[str, str]:
    """The text entries of the call's metadata, by lower-case key."""
    entries: dict[str, str] = {}
    for key, value in context.invocation_metadata() or ():
        if isinstance(value, str):
            name = key.lower()
            entries[name] = f"{entries[name]},{value}" if name in entries else value
    return entries


class GrpcTelemetry:
    """The instruments of one gRPC server; ``call`` and ``stream`` observe one call each."""

    def __init__(
        self,
        meter: Meter,
        *,
        server_address: str | None = None,
        server_port: int | None = None,
        untraced_methods: frozenset[str] = UNTRACED_METHODS,
    ) -> None:
        self._untraced_methods = untraced_methods
        self._server: dict[str, AttributeValue] = {}
        if server_address:
            self._server["server.address"] = server_address
        if server_port is not None:
            self._server["server.port"] = server_port
        self._duration = meter.create_histogram(
            RPC_DURATION_METRIC,
            unit="s",
            description="Duration of gRPC server calls",
            explicit_bucket_boundaries_advisory=_DURATION_BUCKETS,
        )
        self._active_streams = meter.create_up_down_counter(
            GRPC_ACTIVE_STREAMS_METRIC,
            unit="{stream}",
            description="Number of open gRPC Stream calls",
        )

    def call(self, method: str, context: Context) -> ObservedCall:
        """A unary call: its span is current while the ``with`` block runs."""
        return ObservedCall(self, method, context, streaming=False)

    def stream(self, method: str, context: Context) -> ObservedCall:
        """A streaming call: one span, never the current one."""
        return ObservedCall(self, method, context, streaming=True)

    def start(self, method: str, context: Context) -> Span | None:
        if method in self._untraced_methods:
            return None
        metadata = _metadata(context)
        attributes: dict[str, AttributeValue] = {
            "rpc.system.name": RPC_SYSTEM_NAME,
            "rpc.method": method,
            **self._server,
        }
        user_agent = metadata.get("user-agent")
        if user_agent:
            attributes["user_agent.original"] = user_agent[:_USER_AGENT_MAX_LENGTH]
        carrier = {name: metadata[name] for name in _TRACE_ENTRIES if name in metadata}
        return start_span(
            method, kind=SpanKind.SERVER, attributes=attributes, context=trace_context(carrier)
        )

    def record(self, method: str, status: grpc.StatusCode, seconds: float) -> None:
        attributes: dict[str, AttributeValue] = {
            "rpc.system.name": RPC_SYSTEM_NAME,
            "rpc.method": method,
            "rpc.response.status_code": status.name,
        }
        if status in _SERVER_ERROR_STATUSES:
            attributes["error.type"] = status.name
        self._duration.record(seconds, attributes)

    def stream_opened(self) -> None:
        self._active_streams.add(1)

    def stream_closed(self) -> None:
        self._active_streams.add(-1)


class ObservedCall:
    """One call, from the start of its handler to its end. ``fail`` states the
    status a failed call ends with; a call nobody failed ended ``OK``, or
    ``CANCELLED`` when the client went away."""

    def __init__(
        self, telemetry: GrpcTelemetry, method: str, context: Context, *, streaming: bool
    ) -> None:
        self._telemetry = telemetry
        self._method = method
        self._context = context
        self._streaming = streaming
        self._status = grpc.StatusCode.OK
        self._started = 0.0
        self._sent = 0
        self._received = 0
        self._scope: Any = None
        self.span: Span | None = None

    def __enter__(self) -> ObservedCall:
        self._started = perf_counter()
        self.span = self._telemetry.start(self._method, self._context)
        if self._streaming:
            self._telemetry.stream_opened()
        elif self.span is not None:
            # The SDK's own exception recording stays off: an exception
            # reaches the span only through ``fail`` (scrubbed).
            self._scope = trace.use_span(
                self.span, end_on_exit=False, record_exception=False, set_status_on_exception=False
            )
            self._scope.__enter__()
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        if exc is not None:
            # The handler was cancelled or closed: the client ended the call.
            self._status = grpc.StatusCode.CANCELLED
        if self._scope is not None:
            self._scope.__exit__(exc_type, exc, traceback)
        if self._streaming:
            self._telemetry.stream_closed()
        if self.span is not None:
            self.span.set_attribute("rpc.response.status_code", self._status.name)
            if self._streaming:
                self.span.set_attribute("mt5_bridge.grpc.messages_sent", self._sent)
                self.span.set_attribute("mt5_bridge.grpc.messages_received", self._received)
            self.span.end()
        self._telemetry.record(self._method, self._status, perf_counter() - self._started)

    def fail(self, status: grpc.StatusCode, error: Exception, *, unexpected: bool) -> None:
        """The call ends with ``status``. An ``unexpected`` error is recorded
        on the span as an exception event, scrubbed of secrets."""
        self._status = status
        if self.span is None or status not in _SERVER_ERROR_STATUSES:
            return
        if unexpected:
            record_error(self.span, error)
        else:
            self.span.set_attribute("error.type", status.name)
            self.span.set_status(Status(StatusCode.ERROR))

    def message_sent(self) -> None:
        self._sent += 1

    def message_received(self) -> None:
        self._received += 1


__all__ = [
    "GRPC_ACTIVE_STREAMS_METRIC",
    "RPC_DURATION_METRIC",
    "UNTRACED_METHODS",
    "GrpcTelemetry",
    "ObservedCall",
]
