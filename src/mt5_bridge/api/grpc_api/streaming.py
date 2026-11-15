"""The Streaming service: the gRPC form of the WebSocket endpoint ``/v1/stream``.

The subscription state and every publishing rule are the ones in ``api.ws``;
this module only translates. A client message becomes the WebSocket message
it corresponds to, and each OCSF event ``api.ws`` would send on a socket
becomes a ``StreamEvent`` with the same classification, metadata and sequence
(``convert.stream_event``, docs/ocsf.md).

The call ends when the client cancels it. A client that half-closes (stops
sending) keeps receiving what it subscribed to. A client message that is not
valid ends the call with the validation problem, where the WebSocket closes
with code 1008.
"""

import asyncio
import contextlib
from collections.abc import AsyncIterator, Mapping
from typing import Any

from mt5_bridge.api.app_state import AppState
from mt5_bridge.api.grpc_api import convert
from mt5_bridge.api.grpc_api.calls import (
    Service,
    abort,
    failure_for,
    invalid_value,
    peer_address,
    report,
)
from mt5_bridge.api.grpc_api.generated import mt5_bridge_pb2 as pb
from mt5_bridge.api.grpc_api.generated import mt5_bridge_pb2_grpc as pb_grpc
from mt5_bridge.api.grpc_api.telemetry import Context, GrpcTelemetry, ObservedCall
from mt5_bridge.api.telemetry import stream_operation_span
from mt5_bridge.api.ws import (
    DEFAULT_POLL_INTERVAL_SECONDS,
    INVALID_CLIENT_MESSAGE,
    ConnectionState,
    handle_client_message,
    publish_once,
)
from mt5_bridge.core.envelope import StreamPeer

SPAN_NAMESPACE = "mt5_bridge.grpc"

_CHANNEL_NAMES: Mapping[int, str] = {
    pb.CHANNEL_TICKS: "ticks",
    pb.CHANNEL_BARS: "bars",
    pb.CHANNEL_BOOK: "book",
    pb.CHANNEL_EXECUTIONS: "executions",
    pb.CHANNEL_STATUS: "status",
    pb.CHANNEL_ACCOUNT: "account",
}


def _subscription(op: str, message: pb.Subscribe | pb.Unsubscribe) -> dict[str, Any]:
    channel = _CHANNEL_NAMES.get(message.channel)
    if channel is None:
        raise invalid_value("channel", "missing or not a known value")
    result: dict[str, Any] = {"op": op, "channel": channel, "symbols": list(message.symbols)}
    if channel == "bars":
        result["timeframe"] = convert.TIMEFRAME.from_pb(message.timeframe, field="timeframe").value
    return result


def to_ws_message(request: pb.StreamRequest) -> dict[str, Any]:
    """The WebSocket client message ``request`` corresponds to."""
    op = request.WhichOneof("op")
    if op == "subscribe":
        return _subscription("subscribe", request.subscribe)
    if op == "unsubscribe":
        return _subscription("unsubscribe", request.unsubscribe)
    if op == "resume":
        if request.resume.from_seq < 1:
            raise invalid_value("from_seq", "must be >= 1")
        return {"op": "resume", "channel": "executions", "from_seq": request.resume.from_seq}
    if op == "ping":
        return {"op": "ping"}
    raise invalid_value("op", "the request carries no operation")


class _Outbox:
    """Collects what one pass of ``api.ws`` sends, as protobuf messages."""

    def __init__(self) -> None:
        self._pending: list[pb.StreamEvent] = []

    async def send_json(self, data: Any) -> None:
        self._pending.append(convert.stream_event(data))

    def drain(self) -> list[pb.StreamEvent]:
        pending, self._pending = self._pending, []
        return pending


class StreamingService(Service, pb_grpc.StreamingServicer):
    service_name = pb.DESCRIPTOR.services_by_name["Streaming"].full_name

    def __init__(
        self,
        state: AppState,
        telemetry: GrpcTelemetry,
        *,
        poll_interval: float = DEFAULT_POLL_INTERVAL_SECONDS,
    ) -> None:
        super().__init__(state, telemetry)
        self._poll_interval = poll_interval

    async def Stream(
        self, request_iterator: AsyncIterator[pb.StreamRequest], context: Context
    ) -> AsyncIterator[pb.StreamEvent]:
        method = f"{self.service_name}/Stream"
        failure = None
        with self.telemetry.stream(method, context) as call:
            try:
                async for event in self._run(request_iterator, context, call):
                    call.message_sent()
                    yield event
            except Exception as error:
                failure = failure_for(error)
                report(failure, error, method=method)
                call.fail(failure.status, error, unexpected=failure.unexpected)
        if failure is not None:
            await abort(context, failure, method=method)

    async def _run(
        self, requests: AsyncIterator[pb.StreamRequest], context: Context, call: ObservedCall
    ) -> AsyncIterator[pb.StreamEvent]:
        state = self.state
        address = peer_address(context)
        peer = StreamPeer(
            account_id=state.expected_account_id,
            host=address[0] if address is not None else None,
            port=address[1] if address is not None else None,
        )
        connection = ConnectionState()
        outbox = _Outbox()
        # ``None`` marks the end of the client's messages (a half-close).
        inbox: asyncio.Queue[pb.StreamRequest | None] = asyncio.Queue()

        async def read_requests() -> None:
            try:
                async for request in requests:
                    inbox.put_nowait(request)
            finally:
                inbox.put_nowait(None)

        reader = asyncio.create_task(read_requests(), name="mt5-bridge-grpc-stream-reader")
        client_sending = True
        try:
            while True:
                if client_sending:
                    try:
                        request = await asyncio.wait_for(inbox.get(), timeout=self._poll_interval)
                    except TimeoutError:
                        pass
                    else:
                        if request is None:
                            client_sending = False
                        else:
                            call.message_received()
                            await self._handle(request, outbox, connection, peer, call)
                else:
                    await asyncio.sleep(self._poll_interval)
                await publish_once(
                    outbox,
                    connection,
                    peer=peer,
                    worker=state.worker,
                    sequence_log=state.sequence_log,
                    kill_switch=state.kill_switch,
                    terminal_watchdog_state=state.terminal_watchdog_state,
                    trading_day_policy=state.trading_day_policy,
                    auction_window=state.auction_window,
                    clock=state.clock,
                )
                for event in outbox.drain():
                    yield event
        finally:
            reader.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await reader

    async def _handle(
        self,
        request: pb.StreamRequest,
        outbox: _Outbox,
        connection: ConnectionState,
        peer: StreamPeer,
        call: ObservedCall,
    ) -> None:
        message = to_ws_message(request)
        with stream_operation_span(message, connection=call.span, namespace=SPAN_NAMESPACE):
            try:
                await handle_client_message(
                    outbox, message, connection, peer=peer, clock=self.state.clock
                )
            except INVALID_CLIENT_MESSAGE as error:
                raise invalid_value(str(request.WhichOneof("op")), "invalid message") from error
