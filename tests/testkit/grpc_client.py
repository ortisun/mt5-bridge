"""Serve the gRPC API of a test ``AppState`` on a loopback port and call it."""

import asyncio
from collections.abc import AsyncGenerator, Mapping
from contextlib import asynccontextmanager
from typing import Any

import grpc
from google.protobuf.json_format import MessageToDict

from mt5_bridge.api.app_state import AppState
from mt5_bridge.api.grpc_api.calls import PROBLEM_METADATA_KEY
from mt5_bridge.api.grpc_api.generated import mt5_bridge_pb2 as pb
from mt5_bridge.api.grpc_api.server import GrpcServeOptions, start_grpc_server

READ_TIMEOUT_SECONDS = 5.0
SETTLE_TIMEOUT_SECONDS = 2.0


@asynccontextmanager
async def serve_grpc(state: AppState) -> AsyncGenerator[grpc.aio.Channel]:
    """A channel to a server for ``state`` on a port the operating system chose."""
    running = await start_grpc_server(state, GrpcServeOptions(host="127.0.0.1", port=0))
    channel = grpc.aio.insecure_channel(f"127.0.0.1:{running.port}")
    try:
        yield channel
    finally:
        await channel.close()
        await running.server.stop(None)
        await _handlers_ended()


async def _handlers_ended() -> None:
    """The server cancels the handler of a call the client ended on a later
    turn of the loop; wait for it, so its span and its audit record exist
    when the test looks and never land in the next test."""
    others = [task for task in asyncio.all_tasks() if task is not asyncio.current_task()]
    if others:
        await asyncio.wait(others, timeout=SETTLE_TIMEOUT_SECONDS)


def problem_of(error: grpc.aio.AioRpcError) -> pb.Problem:
    """The ``Problem`` a failed call carries in its trailing metadata."""
    for key, value in error.trailing_metadata():
        if key == PROBLEM_METADATA_KEY and isinstance(value, bytes):
            return pb.Problem.FromString(value)
    raise AssertionError(f"the call failed without {PROBLEM_METADATA_KEY}: {error!r}")


async def read_one(
    call: grpc.aio.StreamStreamCall[pb.StreamRequest, pb.StreamEvent],
) -> pb.StreamEvent:
    """The next message of a stream; fails instead of waiting forever."""
    message = await asyncio.wait_for(call.read(), timeout=READ_TIMEOUT_SECONDS)
    assert isinstance(message, pb.StreamEvent), f"the stream ended: {message!r}"
    return message


_OPTIONAL_ATTRIBUTES = ("status_id", "status_code", "status_detail", "message")
_OPTIONAL_METADATA = (
    "log_name",
    "sequence",
    "original_time",
    "processed_time",
    "uid",
    "correlation_uid",
)


def assert_same_ocsf_event(event: pb.StreamEvent, wire: Mapping[str, Any]) -> None:
    """``event`` has the classification, time, outcome and metadata of the
    ``/v1/stream`` message ``wire``; the payload is compared by the caller."""
    for name in ("class_uid", "category_uid", "activity_id", "type_uid", "time", "severity_id"):
        assert getattr(event, name) == wire[name], name
    assert event.type_uid == event.class_uid * 100 + event.activity_id
    assert event.activity_name == wire["activity_name"]
    for name in _OPTIONAL_ATTRIBUTES:
        assert (getattr(event, name) if event.HasField(name) else None) == wire.get(name), name
    metadata: Mapping[str, Any] = wire["metadata"]
    assert event.metadata.version == metadata["version"]
    assert event.metadata.event_code == metadata["event_code"]
    assert (
        MessageToDict(event.metadata.product, preserving_proto_field_name=True)
        == (metadata["product"])
    )
    for name in _OPTIONAL_METADATA:
        value = getattr(event.metadata, name) if event.metadata.HasField(name) else None
        assert value == metadata.get(name), name
    unmapped: Mapping[str, Any] = wire.get("unmapped", {})
    unit, basis = unmapped.get("source_time_unit"), unmapped.get("time_basis")
    assert pb.SourceTimeUnit.Name(event.source_time_unit) == (
        f"SOURCE_TIME_UNIT_{unit.upper()}" if unit else "SOURCE_TIME_UNIT_UNSPECIFIED"
    )
    assert pb.TimeBasis.Name(event.time_basis) == (
        f"TIME_BASIS_{basis.upper()}" if basis else "TIME_BASIS_UNSPECIFIED"
    )
    assert event.HasField("api") is ("api" in wire)
    if "api" in wire:
        assert event.api.operation == wire["api"]["operation"]
        assert MessageToDict(event.actor, preserving_proto_field_name=True) == wire["actor"]
