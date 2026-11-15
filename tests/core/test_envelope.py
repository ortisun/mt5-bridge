"""StreamEnvelope wire rendering: every field, JSON-primitive-safe."""

from datetime import UTC, datetime

from mt5_bridge.core.envelope import StreamEnvelope, to_wire
from mt5_bridge.mt5.models import SourceTimeUnit, TimeBasis


def test_to_wire_renders_every_envelope_field_as_a_json_primitive() -> None:
    envelope = StreamEnvelope(
        channel="ticks",
        stream_id="ticks:WINZ25",
        seq=7,
        ts_utc=datetime(2024, 1, 1, tzinfo=UTC),
        source_time_raw=1_704_067_200_000,
        source_time_unit=SourceTimeUnit.MILLISECONDS,
        time_basis=TimeBasis.EXCHANGE_REPORTED,
        received_at=datetime(2024, 1, 1, 0, 0, 1, tzinfo=UTC),
        payload={"bid": "100.00", "ask": "100.05"},
    )

    wire = to_wire(envelope)

    assert wire == {
        "channel": "ticks",
        "stream_id": "ticks:WINZ25",
        "seq": 7,
        "ts_utc": "2024-01-01T00:00:00+00:00",
        "source_time_raw": 1_704_067_200_000,
        "source_time_unit": "milliseconds",
        "time_basis": "exchange_reported",
        "received_at": "2024-01-01T00:00:01+00:00",
        "payload": {"bid": "100.00", "ask": "100.05"},
    }
    assert all(isinstance(key, str) for key in wire)
