"""Everything the running service emits is a valid OCSF event.

Real ``/v1/stream`` messages of every kind, the audit events of real write
requests and the log lines they produce, validated against the schema
derived from https://schema.ocsf.io (``tools/vendor_ocsf_schema.py``;
``tests/contracts/ocsf/``). No network: the schema is vendored.
"""

import json
import logging
from decimal import Decimal
from typing import Any

import pytest
from jsonschema import Draft7Validator, ValidationError

from mt5_bridge.core import ocsf
from mt5_bridge.core.log_formatting import JsonLogFormatter
from mt5_bridge.mt5.models import BookLevel, BookSide, Timeframe

from testkit.app_builder import build_test_app
from testkit.contracts import assert_is_ocsf_event, load_ocsf_schema
from testkit.fixtures import WINZ25
from testkit.http_client import TypedTestClient


def submit_body(client_order_id: str = "co-1") -> dict[str, object]:
    return {
        "client_order_id": client_order_id,
        "symbol": WINZ25,
        "side": "BUY",
        "type": "MARKET",
        "volume": "1",
        "time_in_force": "DAY",
    }


class TestVendoredSchema:
    def test_is_a_valid_json_schema(self) -> None:
        Draft7Validator.check_schema(load_ocsf_schema())

    def test_is_the_version_the_bridge_declares(self) -> None:
        assert f"ocsf-{ocsf.OCSF_VERSION}-subset" in load_ocsf_schema()["$id"]

    def test_covers_every_class_the_vocabulary_declares(self) -> None:
        schema = load_ocsf_schema()

        assert set(schema["properties"]["class_uid"]["enum"]) == set(ocsf.EVENT_CLASSES)
        for event_class in ocsf.EVENT_CLASSES.values():
            definition = next(
                rule["then"]["$ref"].rsplit("/", 1)[1]
                for rule in schema["allOf"]
                if rule["if"]["properties"]["class_uid"]["const"] == event_class.uid
            )
            properties = schema["$defs"][definition]["properties"]
            assert properties["category_uid"]["const"] == event_class.category_uid
            assert set(properties["activity_id"]["enum"]) == set(event_class.activities)

    def test_rejects_an_event_without_a_required_attribute(self) -> None:
        event = ocsf.event(ocsf.BASE_EVENT, ocsf.BaseActivity.OTHER, activity_name="x", time_ms=1)
        del event["severity_id"]

        with pytest.raises(ValidationError, match="severity_id"):
            assert_is_ocsf_event(event)

    def test_rejects_an_attribute_the_class_does_not_have(self) -> None:
        event = ocsf.event(ocsf.BASE_EVENT, ocsf.BaseActivity.OTHER, activity_name="x", time_ms=1)
        event["channel"] = "ticks"

        with pytest.raises(ValidationError, match="channel"):
            assert_is_ocsf_event(event)


def test_every_stream_message_is_a_valid_ocsf_event() -> None:
    app, fake_client, _state = build_test_app()
    fake_client.set_tick(WINZ25, bid=Decimal(1), ask=Decimal(1), last=Decimal(1), volume=Decimal(1))
    fake_client.push_bar(WINZ25, Timeframe.D1, time=1, o=1, h=1, low=1, c=1, volume=1)
    fake_client.push_bar(WINZ25, Timeframe.D1, time=2, o=1, h=1, low=1, c=1, volume=1)
    fake_client.set_book(
        WINZ25, (BookLevel(side=BookSide.BUY, price=Decimal(100), volume=Decimal(10)),)
    )
    subscriptions: list[dict[str, object]] = [
        {"op": "subscribe", "channel": "ticks", "symbols": [WINZ25]},
        {"op": "subscribe", "channel": "bars", "symbols": [WINZ25], "timeframe": "D1"},
        {"op": "subscribe", "channel": "book", "symbols": [WINZ25]},
        {"op": "subscribe", "channel": "executions"},
        {"op": "subscribe", "channel": "status"},
        {"op": "subscribe", "channel": "account"},
    ]
    messages: list[dict[str, Any]] = []
    with TypedTestClient(app) as http:
        http.post("/v1/orders", json=submit_body())
        with http.websocket_connect("/v1/stream") as ws:
            for subscription in subscriptions:
                ws.send_json(subscription)
            ws.send_json({"op": "ping"})
            while not messages or messages[-1]["metadata"]["event_code"] != "pong":
                messages.append(ws.receive_json())

    seen = {message["metadata"]["event_code"] for message in messages}
    assert seen == {"ticks", "bars", "book", "executions", "status", "account", "pong"}
    for message in messages:
        assert_is_ocsf_event(message)


def test_a_resync_required_message_is_a_valid_ocsf_event() -> None:
    app, fake_client, state = build_test_app()
    del fake_client
    with TypedTestClient(app) as http:
        http.post("/v1/orders", json=submit_body())
        state.sequence_log.prune_no_transaction(stream="executions", keep_last=0)
        with http.websocket_connect("/v1/stream") as ws:
            ws.send_json({"op": "resume", "channel": "executions", "from_seq": 1})
            message = ws.receive_json()

    assert_is_ocsf_event(message)


def test_the_audit_trail_of_real_requests_is_valid_ocsf(
    caplog: pytest.LogCaptureFixture,
) -> None:
    app, fake_client, state = build_test_app()
    del fake_client
    with TypedTestClient(app) as http, caplog.at_level(logging.INFO, logger="mt5_bridge.audit"):
        http.post("/v1/orders", json=submit_body())
        http.post("/v1/kill-switch", json={"action": "HALT_NEW_ORDERS", "reason": "test"})
        http.delete("/v1/kill-switch")

    records = state.audit.list_no_transaction()
    assert [record.method for record in records] == ["POST", "POST", "DELETE"]
    for record in records:
        assert_is_ocsf_event(record.to_ocsf())

    formatter = JsonLogFormatter()
    lines = [
        json.loads(formatter.format(record))
        for record in caplog.records
        if record.name == "mt5_bridge.audit"
    ]
    assert len(lines) == len(records)
    for line, record in zip(lines, records, strict=True):
        assert_is_ocsf_event(line)
        assert line["class_uid"] == 6003
        assert line["time"] == record.to_ocsf()["time"]
        assert line["api"] == {"operation": f"{record.method} {record.path}"}
        assert line["metadata"]["log_name"] == "mt5_bridge.audit"


def test_every_log_line_of_a_running_app_is_a_valid_ocsf_event(
    caplog: pytest.LogCaptureFixture,
) -> None:
    app, fake_client, _state = build_test_app()
    del fake_client
    with TypedTestClient(app) as http, caplog.at_level(logging.DEBUG):
        http.get("/v1/health")
        http.post("/v1/orders", json=submit_body())
        http.post("/v1/orders", json={"client_order_id": "bad"})
        logging.getLogger("mt5_bridge").warning("a plain line with %s", "arguments")
        try:
            raise RuntimeError("boom")
        except RuntimeError:
            logging.getLogger("mt5_bridge").exception("a line with a traceback")

    formatter = JsonLogFormatter(deployment_environment="paper")
    assert caplog.records
    for record in caplog.records:
        assert_is_ocsf_event(json.loads(formatter.format(record)))
