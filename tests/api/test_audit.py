"""AuditLog: each record is stored, is an OCSF API Activity event, and is logged."""

import logging
import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from mt5_bridge.api.audit import AuditLog, AuditRecord
from mt5_bridge.core import ocsf
from mt5_bridge.core.database import BridgeDatabase

from testkit.contracts import assert_is_ocsf_event

NOW = datetime(2024, 1, 1, 0, 0, 1, 500_000, tzinfo=UTC)


def _record(method: str = "POST", outcome: str = "ACKNOWLEDGED") -> AuditRecord:
    return AuditRecord(
        ts_utc=NOW,
        account_id="12345678",
        method=method,
        path="/v1/orders",
        payload_hash="abc123",
        outcome=outcome,
        detail=None,
        src_host="10.0.0.7",
        src_port=5000,
    )


def test_a_record_is_an_api_activity_event() -> None:
    event: Any = _record().to_ocsf()

    assert_is_ocsf_event(event)
    assert event["class_uid"] == 6003
    assert event["class_name"] == "API Activity"
    assert event["activity_id"] == 1
    assert event["activity_name"] == "Create"
    assert event["type_uid"] == 600301
    assert event["time"] == 1_704_067_201_500
    assert event["status_id"] == 1
    assert event["status_code"] == "ACKNOWLEDGED"
    assert event["actor"] == {"user": {"uid": "12345678"}}
    assert event["api"] == {"operation": "POST /v1/orders"}
    assert event["http_request"] == {"http_method": "POST", "url": {"path": "/v1/orders"}}
    assert event["src_endpoint"] == {"ip": "10.0.0.7", "port": 5000}
    assert event["unmapped"] == {"payload_hash": "abc123"}
    assert event["metadata"]["event_code"] == "audit"


@pytest.mark.parametrize(
    ("method", "activity_id"), [("POST", 1), ("PATCH", 3), ("DELETE", 4), ("GET", 2)]
)
def test_the_http_method_sets_the_activity(method: str, activity_id: int) -> None:
    event = _record(method=method).to_ocsf()

    assert_is_ocsf_event(event)
    assert event["activity_id"] == activity_id


@pytest.mark.parametrize(
    ("outcome", "status_id", "severity_id"),
    [
        ("FILLED", 1, 1),
        ("HALTED", 1, 1),
        ("REJECTED", 2, 2),
        ("CANCEL_FAILED", 2, 2),
        ("BOTH_OPEN", 2, 2),
        ("UNKNOWN", 0, 3),
    ],
)
def test_the_outcome_sets_the_status(outcome: str, status_id: int, severity_id: int) -> None:
    event = _record(outcome=outcome).to_ocsf()

    assert_is_ocsf_event(event)
    assert (event["status_id"], event["severity_id"]) == (status_id, severity_id)
    assert event["status_code"] == outcome


def test_a_record_without_a_known_peer_is_still_a_valid_event() -> None:
    audit = AuditLog(BridgeDatabase(":memory:"))

    audit.record(
        now=NOW,
        account_id="12345678",
        method="DELETE",
        path="/v1/kill-switch",
        payload_hash=None,
        outcome="NORMAL",
    )

    (stored,) = audit.list_no_transaction()
    event = stored.to_ocsf()
    assert_is_ocsf_event(event)
    assert event["src_endpoint"] == {"name": "unknown"}
    assert "unmapped" not in event


def test_record_stores_the_peer_and_logs_the_same_event(caplog: pytest.LogCaptureFixture) -> None:
    audit = AuditLog(BridgeDatabase(":memory:"))

    with caplog.at_level(logging.INFO, logger="mt5_bridge.audit"):
        audit.record(
            now=NOW,
            account_id="12345678",
            method="POST",
            path="/v1/orders",
            payload_hash="abc123",
            outcome="ACKNOWLEDGED",
            client=("10.0.0.7", 5000),
        )

    (stored,) = audit.list_no_transaction()
    assert stored == _record()
    (logged,) = caplog.records
    assert logged.__dict__[ocsf.LOG_EXTRA_KEY] == stored.to_ocsf()
    assert logged.getMessage() == "POST /v1/orders: ACKNOWLEDGED"


def test_an_audit_table_from_before_the_peer_columns_gains_them(tmp_path: Path) -> None:
    path = str(tmp_path / "bridge.sqlite")
    with sqlite3.connect(path) as earlier:
        earlier.execute(
            "CREATE TABLE audit_log (id INTEGER PRIMARY KEY AUTOINCREMENT, ts_utc TEXT NOT NULL, "
            "account_id TEXT, method TEXT NOT NULL, path TEXT NOT NULL, payload_hash TEXT, "
            "outcome TEXT NOT NULL, detail TEXT)"
        )
        earlier.execute(
            "INSERT INTO audit_log (ts_utc, account_id, method, path, outcome) "
            "VALUES (?, '12345678', 'POST', '/v1/orders', 'FILLED')",
            (NOW.isoformat(),),
        )

    audit = AuditLog(BridgeDatabase(path))
    audit.record(
        now=NOW,
        account_id="12345678",
        method="DELETE",
        path="/v1/kill-switch",
        payload_hash=None,
        outcome="NORMAL",
        client=("10.0.0.7", 5000),
    )

    old, new = audit.list_no_transaction()
    assert (old.src_host, old.src_port) == (None, None)
    assert (new.src_host, new.src_port) == ("10.0.0.7", 5000)
    assert_is_ocsf_event(old.to_ocsf())
