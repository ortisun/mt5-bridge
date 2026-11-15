"""The message shapes of the WebSocket stream (plan §114), as OCSF events.

Every message the service publishes is an event of the Open Cybersecurity
Schema Framework (``core.ocsf`` owns the vocabulary, ``docs/ocsf.md`` the
mapping table). ``StreamEnvelope`` stays the one domain shape every producer
fills; ``to_wire`` chooses the class by channel:

- ``executions`` is Entity Management: the order is the managed entity and
  the journal outcome sets the activity and the status.
- ``status`` is Application Lifecycle: kill switch, terminal connection and
  trading phase are the state of the application.
- ``ticks``, ``bars``, ``book`` and ``account`` have no class in the schema.
  They are Base Events without the optional captions, the payload under
  ``unmapped``, so a message sent at a high rate stays small.
- the control replies (``pong``, ``RESYNC_REQUIRED``) are API Activity.

``metadata`` carries what a client sequences and correlates by:
``event_code`` (the channel, or the control code), ``log_name`` (the stream
id), ``sequence``, ``original_time`` (the source's raw time, a string) and
``processed_time`` (when the bridge received the datum).

``payload`` must already be JSON-primitive-safe (``str``/``int``/``float``/
``bool``/``None``/nested ``dict``/``list``) by the time it reaches an
envelope: producers convert ``Decimal``/``enum``/``datetime`` fields at the
point they build the payload, not here. This module has no framework and no
opinion on transport; ``api/ws.py`` is the only caller that turns a message
into bytes on a socket.
"""

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime

from mt5_bridge.core import ocsf
from mt5_bridge.core.kill_switch import KillSwitchState
from mt5_bridge.core.oms_state import BridgeOrderState
from mt5_bridge.mt5.models import SourceTimeUnit, TimeBasis

EXECUTIONS_CHANNEL = "executions"
STATUS_CHANNEL = "status"
PONG_EVENT_CODE = "pong"
RESYNC_REQUIRED_EVENT_CODE = "RESYNC_REQUIRED"

ORDER_ENTITY_TYPE = "Order"
STATUS_ACTIVITY_NAME = "State Report"
PING_OPERATION = "ping"
PING_ACTIVITY_NAME = "Ping"
RESYNC_REQUIRED_DETAIL = (
    "the requested sequence is outside the retained window; reconcile through REST, then resume"
)

# The activity label of each channel that has no OCSF class (activity 99, Other).
_UNCLASSIFIED_ACTIVITY_NAMES: Mapping[str, str] = {
    "ticks": "Tick",
    "bars": "Bar",
    "book": "Book Snapshot",
    "account": "Account Snapshot",
}

# A journal outcome as an Entity Management activity: the order is created
# when it is taken, updated while it works, and deleted when it leaves the
# book unfilled. A rejection is a creation that failed.
_ORDER_ACTIVITY: Mapping[str, ocsf.EntityActivity] = {
    BridgeOrderState.RECEIVED: ocsf.EntityActivity.CREATE,
    BridgeOrderState.SUBMITTED: ocsf.EntityActivity.CREATE,
    BridgeOrderState.ACKNOWLEDGED: ocsf.EntityActivity.CREATE,
    BridgeOrderState.REJECTED: ocsf.EntityActivity.CREATE,
    BridgeOrderState.PARTIALLY_FILLED: ocsf.EntityActivity.UPDATE,
    BridgeOrderState.FILLED: ocsf.EntityActivity.UPDATE,
    BridgeOrderState.PENDING_CANCEL: ocsf.EntityActivity.UPDATE,
    BridgeOrderState.PENDING_REPLACE: ocsf.EntityActivity.UPDATE,
    BridgeOrderState.REPLACED: ocsf.EntityActivity.UPDATE,
    BridgeOrderState.CANCELED: ocsf.EntityActivity.DELETE,
    BridgeOrderState.EXPIRED: ocsf.EntityActivity.DELETE,
}


@dataclass(frozen=True, slots=True)
class StreamEnvelope:
    channel: str
    stream_id: str
    seq: int
    ts_utc: datetime
    source_time_raw: int
    source_time_unit: SourceTimeUnit
    time_basis: TimeBasis
    received_at: datetime
    payload: Mapping[str, object]


@dataclass(frozen=True, slots=True)
class StreamPeer:
    """Who a control reply answers: the terminal account the bridge is bound
    to (it does not identify its callers) and the address the connection
    came from."""

    account_id: str
    host: str | None
    port: int | None


def _metadata_attributes(envelope: StreamEnvelope) -> dict[str, object]:
    return {
        "event_code": envelope.channel,
        "log_name": envelope.stream_id,
        "sequence": envelope.seq,
        "original_time": str(envelope.source_time_raw),
        "processed_time": ocsf.epoch_ms(envelope.received_at),
    }


def _time_attributes(envelope: StreamEnvelope) -> dict[str, object]:
    return {
        "source_time_unit": envelope.source_time_unit.value,
        "time_basis": envelope.time_basis.value,
    }


def _execution_to_wire(envelope: StreamEnvelope) -> dict[str, object]:
    payload = dict(envelope.payload)
    client_order_id = str(payload.get("client_order_id"))
    state = str(payload.get("state"))
    retcode = payload.get("mt5_retcode")
    reason = payload.get("reason")
    if state == BridgeOrderState.REJECTED:
        status, level = ocsf.Status.FAILURE, ocsf.Severity.LOW
    elif state in _ORDER_ACTIVITY:
        status, level = ocsf.Status.SUCCESS, ocsf.Severity.INFORMATIONAL
    else:  # UNKNOWN: the outcome must be reconciled, never guessed (plan §56)
        status, level = ocsf.Status.UNKNOWN, ocsf.Severity.HIGH
    metadata_attributes = _metadata_attributes(envelope)
    metadata_attributes["uid"] = f"{envelope.stream_id}:{envelope.seq}"
    metadata_attributes["correlation_uid"] = client_order_id
    return ocsf.event(
        ocsf.ENTITY_MANAGEMENT,
        _ORDER_ACTIVITY.get(state, ocsf.EntityActivity.UNKNOWN),
        time_ms=ocsf.epoch_ms(envelope.ts_utc),
        level=level,
        status=status,
        status_code=None if retcode is None else str(retcode),
        status_detail=None if reason is None else str(reason),
        message=f"order {client_order_id} is {state}",
        metadata_attributes=metadata_attributes,
        attributes={"entity": ocsf.managed_entity(ORDER_ENTITY_TYPE, client_order_id, payload)},
        unmapped=_time_attributes(envelope),
    )


def _status_to_wire(envelope: StreamEnvelope) -> dict[str, object]:
    payload = dict(envelope.payload)
    kill_switch_state = payload.get("kill_switch_state")
    connected = payload.get("terminal_connected") is True
    healthy = kill_switch_state == KillSwitchState.NORMAL and connected
    message = (
        f"kill switch {kill_switch_state}; terminal {'connected' if connected else 'disconnected'}"
    )
    if "symbol" in payload:
        message += f"; {payload['symbol']} trading phase {payload.get('trading_phase')}"
    return ocsf.event(
        ocsf.APPLICATION_LIFECYCLE,
        ocsf.LifecycleActivity.OTHER,
        activity_name=STATUS_ACTIVITY_NAME,
        time_ms=ocsf.epoch_ms(envelope.ts_utc),
        level=ocsf.Severity.INFORMATIONAL if healthy else ocsf.Severity.HIGH,
        message=message,
        metadata_attributes=_metadata_attributes(envelope),
        attributes={"app": ocsf.product()},
        unmapped={**_time_attributes(envelope), "payload": payload},
    )


def to_wire(envelope: StreamEnvelope) -> dict[str, object]:
    """Render an envelope as a JSON-ready OCSF event."""
    if envelope.channel == EXECUTIONS_CHANNEL:
        return _execution_to_wire(envelope)
    if envelope.channel == STATUS_CHANNEL:
        return _status_to_wire(envelope)
    return ocsf.event(
        ocsf.BASE_EVENT,
        ocsf.BaseActivity.OTHER,
        activity_name=_UNCLASSIFIED_ACTIVITY_NAMES.get(envelope.channel, envelope.channel),
        captions=False,
        time_ms=ocsf.epoch_ms(envelope.ts_utc),
        metadata_attributes=_metadata_attributes(envelope),
        unmapped={**_time_attributes(envelope), "payload": dict(envelope.payload)},
    )


def _control_attributes(peer: StreamPeer, operation: str) -> dict[str, object]:
    return {
        "actor": ocsf.actor(peer.account_id),
        "api": ocsf.api(operation),
        "src_endpoint": ocsf.network_endpoint(peer.host, peer.port),
    }


def pong_to_wire(peer: StreamPeer, *, now: datetime) -> dict[str, object]:
    """The reply to a ``ping``: the client's call succeeded."""
    return ocsf.event(
        ocsf.API_ACTIVITY,
        ocsf.ApiActivity.OTHER,
        activity_name=PING_ACTIVITY_NAME,
        time_ms=ocsf.epoch_ms(now),
        status=ocsf.Status.SUCCESS,
        metadata_attributes={"event_code": PONG_EVENT_CODE},
        attributes=_control_attributes(peer, PING_OPERATION),
    )


def resync_required_to_wire(
    peer: StreamPeer, *, stream_id: str, operation: str, now: datetime
) -> dict[str, object]:
    """Sent instead of events when the requested sequence fell outside the
    retained window (plan §114): the client's read of the stream failed."""
    return ocsf.event(
        ocsf.API_ACTIVITY,
        ocsf.ApiActivity.READ,
        time_ms=ocsf.epoch_ms(now),
        level=ocsf.Severity.MEDIUM,
        status=ocsf.Status.FAILURE,
        status_code=RESYNC_REQUIRED_EVENT_CODE,
        status_detail=RESYNC_REQUIRED_DETAIL,
        metadata_attributes={"event_code": RESYNC_REQUIRED_EVENT_CODE, "log_name": stream_id},
        attributes=_control_attributes(peer, operation),
    )
