"""StreamEnvelope wire rendering: an OCSF event per channel, nothing lost."""

from datetime import UTC, datetime
from typing import Any

import pytest

from mt5_bridge.core import ocsf
from mt5_bridge.core.envelope import (
    StreamEnvelope,
    StreamPeer,
    pong_to_wire,
    resync_required_to_wire,
    to_wire,
)
from mt5_bridge.mt5.models import SourceTimeUnit, TimeBasis

from testkit.contracts import assert_is_ocsf_event

NOW = datetime(2024, 1, 1, 0, 0, 1, tzinfo=UTC)
PEER = StreamPeer(account_id="12345678", host="10.0.0.7", port=5000)


def _envelope(channel: str, payload: dict[str, object], *, stream_id: str | None = None) -> Any:
    return to_wire(
        StreamEnvelope(
            channel=channel,
            stream_id=stream_id or channel,
            seq=7,
            ts_utc=datetime(2024, 1, 1, tzinfo=UTC),
            source_time_raw=1_704_067_200_000,
            source_time_unit=SourceTimeUnit.MILLISECONDS,
            time_basis=TimeBasis.EXCHANGE_REPORTED,
            received_at=NOW,
            payload=payload,
        )
    )


def _execution(state: str, *, retcode: int | None = None, reason: str | None = None) -> Any:
    return _envelope(
        "executions",
        {
            "client_order_id": "co-1",
            "symbol": "WINZ25",
            "state": state,
            "ticket": 42,
            "mt5_retcode": retcode,
            "reason": reason,
        },
    )


def test_a_market_data_envelope_is_a_base_event_with_only_the_required_attributes() -> None:
    wire = _envelope("ticks", {"bid": "100.00", "ask": "100.05"}, stream_id="ticks:WINZ25")

    assert wire == {
        "class_uid": 0,
        "category_uid": 0,
        "activity_id": 99,
        "type_uid": 99,
        "activity_name": "Tick",
        "time": 1_704_067_200_000,
        "severity_id": 1,
        "metadata": {
            "version": "1.9.0",
            "product": ocsf.product(),
            "event_code": "ticks",
            "log_name": "ticks:WINZ25",
            "sequence": 7,
            "original_time": "1704067200000",
            "processed_time": 1_704_067_201_000,
        },
        "unmapped": {
            "source_time_unit": "milliseconds",
            "time_basis": "exchange_reported",
            "payload": {"bid": "100.00", "ask": "100.05"},
        },
    }
    assert_is_ocsf_event(wire)


@pytest.mark.parametrize(
    ("channel", "activity_name"),
    [("bars", "Bar"), ("book", "Book Snapshot"), ("account", "Account Snapshot")],
)
def test_every_channel_without_an_ocsf_class_is_a_base_event(
    channel: str, activity_name: str
) -> None:
    wire = _envelope(channel, {"symbol": "WINZ25"})

    assert_is_ocsf_event(wire)
    assert (wire["class_uid"], wire["activity_name"]) == (0, activity_name)
    assert wire["metadata"]["event_code"] == channel


@pytest.mark.parametrize(
    ("state", "activity_id", "status_id", "severity_id"),
    [
        ("ACKNOWLEDGED", 1, 1, 1),
        ("REJECTED", 1, 2, 2),
        ("FILLED", 3, 1, 1),
        ("CANCELED", 4, 1, 1),
        ("UNKNOWN", 0, 0, 4),
    ],
)
def test_an_execution_is_an_entity_management_event_of_the_order(
    state: str, activity_id: int, status_id: int, severity_id: int
) -> None:
    wire = _execution(state)

    assert_is_ocsf_event(wire)
    assert wire["class_uid"] == 3004
    assert wire["class_name"] == "Entity Management"
    assert (wire["activity_id"], wire["status_id"], wire["severity_id"]) == (
        activity_id,
        status_id,
        severity_id,
    )
    assert wire["entity"] == {
        "type_id": 99,
        "type": "Order",
        "uid": "co-1",
        "data": {
            "client_order_id": "co-1",
            "symbol": "WINZ25",
            "state": state,
            "ticket": 42,
            "mt5_retcode": None,
            "reason": None,
        },
    }
    assert wire["metadata"]["sequence"] == 7
    assert wire["metadata"]["uid"] == "executions:7"
    assert wire["metadata"]["correlation_uid"] == "co-1"
    assert wire["unmapped"] == {
        "source_time_unit": "milliseconds",
        "time_basis": "exchange_reported",
    }
    assert "status_code" not in wire
    assert "status_detail" not in wire


def test_an_executions_retcode_and_reason_are_the_status_code_and_detail() -> None:
    wire = _execution("REJECTED", retcode=10006, reason="no money")

    assert_is_ocsf_event(wire)
    assert wire["status_code"] == "10006"
    assert wire["status_detail"] == "no money"
    assert wire["entity"]["data"]["mt5_retcode"] == 10006


@pytest.mark.parametrize(
    ("kill_switch_state", "connected", "severity_id"),
    [("NORMAL", True, 1), ("HALTED", True, 4), ("NORMAL", False, 4)],
)
def test_a_status_envelope_is_an_application_lifecycle_event(
    kill_switch_state: str, connected: bool, severity_id: int
) -> None:
    payload: dict[str, object] = {
        "kill_switch_state": kill_switch_state,
        "terminal_connected": connected,
    }

    wire = _envelope("status", payload)

    assert_is_ocsf_event(wire)
    assert wire["class_uid"] == 6002
    assert wire["activity_name"] == "State Report"
    assert wire["severity_id"] == severity_id
    assert wire["app"] == ocsf.product()
    assert wire["unmapped"]["payload"] == payload


def test_pong_is_a_successful_api_activity() -> None:
    wire: Any = pong_to_wire(PEER, now=NOW)

    assert_is_ocsf_event(wire)
    assert wire["class_uid"] == 6003
    assert wire["activity_name"] == "Ping"
    assert wire["status_id"] == 1
    assert wire["time"] == 1_704_067_201_000
    assert wire["metadata"]["event_code"] == "pong"
    assert wire["api"] == {"operation": "ping"}
    assert wire["actor"] == {"user": {"uid": "12345678"}}
    assert wire["src_endpoint"] == {"ip": "10.0.0.7", "port": 5000}


def test_resync_required_is_a_failed_read_of_the_stream() -> None:
    wire: Any = resync_required_to_wire(PEER, stream_id="executions", operation="resume", now=NOW)

    assert_is_ocsf_event(wire)
    assert (wire["class_uid"], wire["activity_id"], wire["status_id"]) == (6003, 2, 2)
    assert wire["status_code"] == "RESYNC_REQUIRED"
    assert wire["metadata"]["event_code"] == "RESYNC_REQUIRED"
    assert wire["metadata"]["log_name"] == "executions"
    assert wire["api"] == {"operation": "resume"}
