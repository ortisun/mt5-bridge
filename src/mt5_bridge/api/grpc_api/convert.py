"""Conversions between the bridge's own types and the protobuf messages.

Enums map by name: the member ``OrderSide.BUY`` is ``ORDER_SIDE_BUY``.
Decimals travel as strings (plan rule 33), exactly as in the REST contract.
"""

from collections.abc import Mapping
from datetime import datetime
from decimal import Decimal, InvalidOperation
from enum import Enum
from typing import Any, Protocol

from google.protobuf.message import Message
from google.protobuf.timestamp_pb2 import Timestamp

from mt5_bridge.api.grpc_api.calls import invalid_value
from mt5_bridge.api.grpc_api.generated import mt5_bridge_pb2 as pb
from mt5_bridge.core.envelope import EXECUTIONS_CHANNEL, STATUS_CHANNEL
from mt5_bridge.core.kill_switch import KillSwitchAction, KillSwitchState, KillSwitchStatus
from mt5_bridge.core.oms_state import BridgeOrderState, ReplaceOutcome
from mt5_bridge.core.order_journal import JournalEntry
from mt5_bridge.mt5 import models


class _PbEnum[V](Protocol):
    def Name(self, number: V) -> str: ...
    def Value(self, name: str) -> V: ...


class EnumCodec[E: Enum, V]:
    def __init__(self, members: type[E], wrapper: _PbEnum[V], prefix: str) -> None:
        self._members = members
        self._wrapper = wrapper
        self._prefix = prefix

    def to_pb(self, member: E) -> V:
        return self._wrapper.Value(self._prefix + member.name)

    def from_pb(self, value: V, *, field: str) -> E:
        """Rejects the unspecified value and a number this contract does not define."""
        try:
            name = self._wrapper.Name(value).removeprefix(self._prefix)
            return self._members[name]
        except (ValueError, KeyError) as error:
            raise invalid_value(field, "missing or not a known value") from error


ORDER_SIDE = EnumCodec(models.OrderSide, pb.OrderSide, "ORDER_SIDE_")
ORDER_TYPE = EnumCodec(models.OrderType, pb.OrderType, "ORDER_TYPE_")
TIME_IN_FORCE = EnumCodec(models.TimeInForce, pb.TimeInForce, "TIME_IN_FORCE_")
FILLING_MODE = EnumCodec(models.FillingMode, pb.FillingMode, "FILLING_MODE_")
POSITION_ACCOUNTING = EnumCodec(
    models.PositionAccounting, pb.PositionAccounting, "POSITION_ACCOUNTING_"
)
TRADING_PHASE = EnumCodec(models.TradingPhase, pb.TradingPhase, "TRADING_PHASE_")
ORDER_STATE = EnumCodec(BridgeOrderState, pb.OrderState, "ORDER_STATE_")
REPLACE_OUTCOME = EnumCodec(ReplaceOutcome, pb.ReplaceOutcome, "REPLACE_OUTCOME_")
KILL_SWITCH_ACTION = EnumCodec(KillSwitchAction, pb.KillSwitchAction, "KILL_SWITCH_ACTION_")
KILL_SWITCH_STATE = EnumCodec(KillSwitchState, pb.KillSwitchState, "KILL_SWITCH_STATE_")
TIMEFRAME = EnumCodec(models.Timeframe, pb.Timeframe, "TIMEFRAME_")
BOOK_SIDE = EnumCodec(models.BookSide, pb.BookSide, "BOOK_SIDE_")
SOURCE_TIME_UNIT = EnumCodec(models.SourceTimeUnit, pb.SourceTimeUnit, "SOURCE_TIME_UNIT_")
TIME_BASIS = EnumCodec(models.TimeBasis, pb.TimeBasis, "TIME_BASIS_")


def decimal_from(value: str, *, field: str) -> Decimal:
    try:
        return Decimal(value)
    except InvalidOperation as error:
        raise invalid_value(field, f"{value!r} is not a valid decimal string") from error


def timestamp(moment: datetime) -> Timestamp:
    result = Timestamp()
    result.FromDatetime(moment)
    return result


def order(entry: JournalEntry) -> pb.Order:
    return pb.Order(
        client_order_id=entry.client_order_id,
        symbol=entry.symbol,
        side=ORDER_SIDE.to_pb(entry.side),
        order_type=ORDER_TYPE.to_pb(entry.order_type),
        volume=str(entry.volume),
        price=None if entry.price is None else str(entry.price),
        time_in_force=TIME_IN_FORCE.to_pb(entry.time_in_force),
        state=ORDER_STATE.to_pb(entry.state),
        ticket=entry.ticket,
        mt5_retcode=entry.mt5_retcode,
        mt5_last_error=entry.mt5_last_error,
        reason=entry.reason,
    )


def tick(source: models.Tick) -> pb.Tick:
    return pb.Tick(
        symbol=source.symbol,
        time_msc=source.time_msc,
        bid=str(source.bid),
        ask=str(source.ask),
        last=str(source.last),
        volume=str(source.volume),
    )


def kill_switch_status(status: KillSwitchStatus) -> pb.KillSwitchStatus:
    return pb.KillSwitchStatus(
        state=KILL_SWITCH_STATE.to_pb(status.state),
        reason=status.reason,
        updated_at=timestamp(status.updated_at),
    )


def stream_event(wire: Mapping[str, Any]) -> pb.StreamEvent:
    """The ``StreamEvent`` of a message of ``/v1/stream``: the OCSF event
    ``core.envelope`` built, attribute by attribute (docs/ocsf.md). The
    captions are left out; the identifiers say the same."""
    metadata: Mapping[str, Any] = wire["metadata"]
    unmapped: Mapping[str, Any] = wire.get("unmapped", {})
    event = pb.StreamEvent(
        class_uid=wire["class_uid"],
        category_uid=wire["category_uid"],
        activity_id=wire["activity_id"],
        type_uid=wire["type_uid"],
        activity_name=wire["activity_name"],
        time=wire["time"],
        severity_id=wire["severity_id"],
        status_id=wire.get("status_id"),
        status_code=wire.get("status_code"),
        status_detail=wire.get("status_detail"),
        message=wire.get("message"),
        metadata=pb.EventMetadata(
            version=metadata["version"],
            product=pb.Product(**metadata["product"]),
            event_code=metadata["event_code"],
            log_name=metadata.get("log_name"),
            sequence=metadata.get("sequence"),
            original_time=metadata.get("original_time"),
            processed_time=metadata.get("processed_time"),
            uid=metadata.get("uid"),
            correlation_uid=metadata.get("correlation_uid"),
        ),
    )
    if "api" in wire:
        user: Mapping[str, Any] = wire["actor"]["user"]
        endpoint: Mapping[str, Any] = wire["src_endpoint"]
        event.api.operation = wire["api"]["operation"]
        event.actor.user.CopyFrom(pb.User(uid=user.get("uid"), name=user.get("name")))
        event.src_endpoint.CopyFrom(
            pb.NetworkEndpoint(
                ip=endpoint.get("ip"), name=endpoint.get("name"), port=endpoint.get("port")
            )
        )
    if "source_time_unit" in unmapped:
        event.source_time_unit = SOURCE_TIME_UNIT.to_pb(
            models.SourceTimeUnit(unmapped["source_time_unit"])
        )
        event.time_basis = TIME_BASIS.to_pb(models.TimeBasis(unmapped["time_basis"]))
    channel = metadata["event_code"]
    if channel == EXECUTIONS_CHANNEL:
        _set_payload(event, channel, wire["entity"]["data"])
    elif "payload" in unmapped:
        _set_payload(event, channel, unmapped["payload"])
    return event


def _set_payload(event: pb.StreamEvent, channel: str, payload: Mapping[str, Any]) -> None:
    if channel == "ticks":
        event.tick.CopyFrom(pb.TickPayload(**payload))
    elif channel == "bars":
        event.bar.CopyFrom(
            pb.BarPayload(
                symbol=payload["symbol"],
                timeframe=TIMEFRAME.to_pb(models.Timeframe(payload["timeframe"])),
                open=payload["open"],
                high=payload["high"],
                low=payload["low"],
                close=payload["close"],
                volume=payload["volume"],
            )
        )
    elif channel == "book":
        event.book.CopyFrom(
            pb.BookPayload(
                symbol=payload["symbol"],
                levels=[
                    pb.BookLevel(
                        side=BOOK_SIDE.to_pb(models.BookSide(level["side"])),
                        price=level["price"],
                        volume=level["volume"],
                    )
                    for level in payload["levels"]
                ],
            )
        )
    elif channel == EXECUTIONS_CHANNEL:
        event.execution.CopyFrom(
            pb.ExecutionPayload(
                client_order_id=payload["client_order_id"],
                symbol=payload["symbol"],
                state=ORDER_STATE.to_pb(BridgeOrderState(payload["state"])),
                ticket=payload.get("ticket"),
                mt5_retcode=payload.get("mt5_retcode"),
                reason=payload.get("reason"),
            )
        )
    elif channel == STATUS_CHANNEL:
        phase = payload.get("trading_phase")
        event.status.CopyFrom(
            pb.StatusPayload(
                kill_switch_state=KILL_SWITCH_STATE.to_pb(
                    KillSwitchState(payload["kill_switch_state"])
                ),
                terminal_connected=payload["terminal_connected"],
                symbol=payload.get("symbol"),
                trading_phase=(
                    None if phase is None else TRADING_PHASE.to_pb(models.TradingPhase(phase))
                ),
            )
        )
    elif channel == "account":
        event.account.CopyFrom(pb.AccountPayload(**payload))


def optional_decimal(message: Message, field: str) -> Decimal | None:
    """The proto3 ``optional string`` ``field`` as a decimal; ``None`` when absent."""
    if not message.HasField(field):
        return None
    return decimal_from(getattr(message, field), field=field)


def optional_int(message: Message, field: str) -> int | None:
    if not message.HasField(field):
        return None
    return int(getattr(message, field))
