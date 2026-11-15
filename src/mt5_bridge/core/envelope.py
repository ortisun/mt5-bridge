"""The one message shape every WebSocket event goes out in (plan §114).

``payload`` must already be JSON-primitive-safe (``str``/``int``/``float``/
``bool``/``None``/nested ``dict``/``list``) by the time it reaches an
envelope: producers convert ``Decimal``/``enum``/``datetime`` fields at the
point they build the payload, not here. This module has no framework and no
opinion on transport; ``api/ws.py`` is the only caller that turns a
``StreamEnvelope`` into bytes on a socket.
"""

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime

from mt5_bridge.mt5.models import SourceTimeUnit, TimeBasis


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


def to_wire(envelope: StreamEnvelope) -> dict[str, object]:
    """Render an envelope as a JSON-ready ``dict``."""
    return {
        "channel": envelope.channel,
        "stream_id": envelope.stream_id,
        "seq": envelope.seq,
        "ts_utc": envelope.ts_utc.isoformat(),
        "source_time_raw": envelope.source_time_raw,
        "source_time_unit": envelope.source_time_unit.value,
        "time_basis": envelope.time_basis.value,
        "received_at": envelope.received_at.isoformat(),
        "payload": dict(envelope.payload),
    }
