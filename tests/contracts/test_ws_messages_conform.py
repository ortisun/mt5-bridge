"""Real /v1/stream frames, from a running connection, validated against
ws_messages.schema.json.
"""

from decimal import Decimal

from mt5_bridge.mt5.models import BookLevel, BookSide, Timeframe

from testkit.app_builder import build_test_app
from testkit.contracts import assert_conforms, load_ws_schema, ws_schema_validator
from testkit.fixtures import WINZ25
from testkit.http_client import TypedTestClient

SCHEMA = load_ws_schema()


def submit_body(client_order_id: str = "co-1") -> dict[str, object]:
    return {
        "client_order_id": client_order_id,
        "symbol": WINZ25,
        "side": "BUY",
        "type": "MARKET",
        "volume": "1",
        "time_in_force": "DAY",
    }


def test_a_client_subscribe_message_conforms() -> None:
    validator = ws_schema_validator(SCHEMA, "SubscribeMessage")

    assert_conforms(
        validator, {"op": "subscribe", "channel": "bars", "symbols": [WINZ25], "timeframe": "D1"}
    )


def test_a_client_ping_message_conforms() -> None:
    validator = ws_schema_validator(SCHEMA, "PingMessage")

    assert_conforms(validator, {"op": "ping"})


def test_the_real_pong_reply_conforms() -> None:
    app, fake_client, _state = build_test_app()
    del fake_client
    validator = ws_schema_validator(SCHEMA, "PongMessage")
    with TypedTestClient(app) as http:
        with http.websocket_connect("/v1/stream") as ws:
            ws.send_json({"op": "ping"})
            message = ws.receive_json()

    assert_conforms(validator, message)


def test_a_real_execution_envelope_conforms() -> None:
    app, fake_client, _state = build_test_app()
    del fake_client
    validator = ws_schema_validator(SCHEMA, "StreamEnvelope")
    with TypedTestClient(app) as http:
        with http.websocket_connect("/v1/stream") as ws:
            ws.send_json({"op": "subscribe", "channel": "executions"})
            http.post("/v1/orders", json=submit_body())
            message = ws.receive_json()

    assert_conforms(validator, message)


def test_a_real_tick_envelope_conforms() -> None:
    app, fake_client, _state = build_test_app()
    fake_client.set_tick(WINZ25, bid=Decimal(1), ask=Decimal(1), last=Decimal(1), volume=Decimal(1))
    validator = ws_schema_validator(SCHEMA, "StreamEnvelope")
    with TypedTestClient(app) as http:
        with http.websocket_connect("/v1/stream") as ws:
            ws.send_json({"op": "subscribe", "channel": "ticks", "symbols": [WINZ25]})
            message = ws.receive_json()

    assert_conforms(validator, message)


def test_a_real_bar_envelope_conforms() -> None:
    app, fake_client, _state = build_test_app()
    # Two pushes: the first closes once the second opens (rule 28), and only
    # a closed bar is ever emitted.
    fake_client.push_bar(WINZ25, Timeframe.D1, time=1, o=1, h=1, low=1, c=1, volume=1)
    fake_client.push_bar(WINZ25, Timeframe.D1, time=2, o=1, h=1, low=1, c=1, volume=1)
    validator = ws_schema_validator(SCHEMA, "StreamEnvelope")
    with TypedTestClient(app) as http:
        with http.websocket_connect("/v1/stream") as ws:
            ws.send_json(
                {"op": "subscribe", "channel": "bars", "symbols": [WINZ25], "timeframe": "D1"}
            )
            message = ws.receive_json()

    assert_conforms(validator, message)


def test_a_real_book_envelope_conforms() -> None:
    app, fake_client, _state = build_test_app()
    fake_client.set_book(
        WINZ25, (BookLevel(side=BookSide.BUY, price=Decimal(100), volume=Decimal(10)),)
    )
    validator = ws_schema_validator(SCHEMA, "StreamEnvelope")
    with TypedTestClient(app) as http:
        with http.websocket_connect("/v1/stream") as ws:
            ws.send_json({"op": "subscribe", "channel": "book", "symbols": [WINZ25]})
            message = ws.receive_json()

    assert_conforms(validator, message)


def test_a_real_status_envelope_conforms() -> None:
    app, fake_client, _state = build_test_app()
    del fake_client
    validator = ws_schema_validator(SCHEMA, "StreamEnvelope")
    with TypedTestClient(app) as http:
        with http.websocket_connect("/v1/stream") as ws:
            ws.send_json({"op": "subscribe", "channel": "status"})
            message = ws.receive_json()

    assert_conforms(validator, message)
    assert message["unmapped"]["payload"]["terminal_connected"] is True


def test_a_status_message_reports_trading_phase_for_a_watched_symbol() -> None:
    """audit F4b, plan section 114: the status channel publishes
    trading-phase changes for symbols this connection already watches
    through a market-data channel (never a broker-confirmed exchange phase,
    only this bridge's own session-window approximation)."""
    app, fake_client, _state = build_test_app()
    del fake_client
    validator = ws_schema_validator(SCHEMA, "StreamEnvelope")
    with TypedTestClient(app) as http:
        with http.websocket_connect("/v1/stream") as ws:
            ws.send_json({"op": "subscribe", "channel": "status"})
            first = ws.receive_json()  # the base kill-switch/terminal_connected snapshot
            ws.send_json(
                {"op": "subscribe", "channel": "bars", "symbols": [WINZ25], "timeframe": "D1"}
            )
            second = ws.receive_json()  # the phase evidence, now that a symbol is watched
            messages = [first, second]

    phase_messages = [
        message
        for message in messages
        if message["metadata"]["event_code"] == "status"
        and "symbol" in message["unmapped"]["payload"]
    ]
    assert phase_messages, f"no phase status message among {messages!r}"
    for message in phase_messages:
        assert_conforms(validator, message)
    assert phase_messages[0]["unmapped"]["payload"]["symbol"] == WINZ25
    assert phase_messages[0]["unmapped"]["payload"]["trading_phase"] == "CLOSED"


def test_a_real_account_envelope_conforms() -> None:
    app, fake_client, _state = build_test_app()
    del fake_client
    validator = ws_schema_validator(SCHEMA, "StreamEnvelope")
    with TypedTestClient(app) as http:
        with http.websocket_connect("/v1/stream") as ws:
            ws.send_json({"op": "subscribe", "channel": "account"})
            message = ws.receive_json()

    assert_conforms(validator, message)


def test_a_real_resync_required_message_conforms() -> None:
    app, fake_client, state = build_test_app()
    del fake_client
    validator = ws_schema_validator(SCHEMA, "ResyncRequiredMessage")
    with TypedTestClient(app) as http:
        http.post("/v1/orders", json=submit_body())
        state.sequence_log.prune_no_transaction(stream="executions", keep_last=0)
        with http.websocket_connect("/v1/stream") as ws:
            ws.send_json({"op": "resume", "channel": "executions", "from_seq": 1})
            message = ws.receive_json()

    assert_conforms(validator, message)
