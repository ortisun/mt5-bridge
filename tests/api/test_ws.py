"""/v1/stream: ping, closed-bars-only, executions resume."""

from decimal import Decimal

from mt5_bridge.mt5.models import BookLevel, BookSide, Timeframe

from testkit.app_builder import build_test_app
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


def test_ping_receives_pong() -> None:
    app, fake_client, _state = build_test_app()
    del fake_client
    with TypedTestClient(app) as http:
        with http.websocket_connect("/v1/stream") as ws:
            ws.send_json({"op": "ping"})
            message = ws.receive_json()

    assert message == {"type": "pong"}


def test_bars_channel_only_ever_delivers_closed_bars() -> None:
    app, client, _state = build_test_app()
    client.push_bar(WINZ25, Timeframe.D1, time=1, o=1, h=2, low=1, c=1, volume=10)
    with TypedTestClient(app) as http:
        with http.websocket_connect("/v1/stream") as ws:
            ws.send_json(
                {"op": "subscribe", "channel": "bars", "symbols": [WINZ25], "timeframe": "D1"}
            )
            client.push_bar(WINZ25, Timeframe.D1, time=2, o=1, h=2, low=1, c=2, volume=10)
            message = ws.receive_json()

    assert message["channel"] == "bars"
    assert message["payload"]["close"] == "1"


def test_book_channel_delivers_a_snapshot_on_subscribe() -> None:
    app, client, _state = build_test_app()
    client.set_book(WINZ25, (BookLevel(side=BookSide.BUY, price=Decimal(100), volume=Decimal(10)),))
    with TypedTestClient(app) as http:
        with http.websocket_connect("/v1/stream") as ws:
            ws.send_json({"op": "subscribe", "channel": "book", "symbols": [WINZ25]})
            message = ws.receive_json()

    assert message["channel"] == "book"
    assert message["payload"]["symbol"] == WINZ25
    assert message["payload"]["levels"] == [{"side": "BUY", "price": "100", "volume": "10"}]


def test_book_channel_does_not_repeat_an_unchanged_snapshot() -> None:
    app, client, _state = build_test_app()
    client.set_book(WINZ25, (BookLevel(side=BookSide.BUY, price=Decimal(100), volume=Decimal(10)),))
    with TypedTestClient(app) as http:
        with http.websocket_connect("/v1/stream") as ws:
            ws.send_json({"op": "subscribe", "channel": "book", "symbols": [WINZ25]})
            first = ws.receive_json()
            ws.send_json({"op": "ping"})
            second = ws.receive_json()

    assert first["channel"] == "book"
    assert second == {"type": "pong"}


def test_a_symbol_with_no_depth_publishes_nothing_on_the_book_channel() -> None:
    app, client, _state = build_test_app()
    del client  # order_book returns None for every symbol until set_book is called
    with TypedTestClient(app) as http:
        with http.websocket_connect("/v1/stream") as ws:
            ws.send_json({"op": "subscribe", "channel": "book", "symbols": [WINZ25]})
            ws.send_json({"op": "ping"})
            message = ws.receive_json()

    assert message == {"type": "pong"}


def test_executions_subscribe_delivers_a_submitted_order_event() -> None:
    app, fake_client, _state = build_test_app()
    del fake_client
    with TypedTestClient(app) as http:
        with http.websocket_connect("/v1/stream") as ws:
            ws.send_json({"op": "subscribe", "channel": "executions"})
            http.post("/v1/orders", json=submit_body())
            message = ws.receive_json()

    assert message["channel"] == "executions"
    assert message["payload"]["client_order_id"] == "co-1"


def test_resume_with_an_expired_cursor_returns_resync_required() -> None:
    app, fake_client, state = build_test_app()
    del fake_client
    with TypedTestClient(app) as http:
        http.post("/v1/orders", json=submit_body())
        state.sequence_log.prune_no_transaction(stream="executions", keep_last=0)

        with http.websocket_connect("/v1/stream") as ws:
            ws.send_json({"op": "resume", "channel": "executions", "from_seq": 1})
            message = ws.receive_json()

    assert message == {"channel": "executions", "type": "RESYNC_REQUIRED"}


def test_account_channel_delivers_a_snapshot_on_subscribe() -> None:
    app, fake_client, _state = build_test_app()
    del fake_client
    with TypedTestClient(app) as http:
        with http.websocket_connect("/v1/stream") as ws:
            ws.send_json({"op": "subscribe", "channel": "account"})
            message = ws.receive_json()

    assert message["channel"] == "account"
    assert "balance" in message["payload"]


def test_account_channel_does_not_repeat_an_unchanged_snapshot() -> None:
    app, fake_client, _state = build_test_app()
    del fake_client
    with TypedTestClient(app) as http:
        with http.websocket_connect("/v1/stream") as ws:
            ws.send_json({"op": "subscribe", "channel": "account"})
            first = ws.receive_json()
            ws.send_json({"op": "ping"})
            second = ws.receive_json()

    assert first["channel"] == "account"
    assert second == {"type": "pong"}
