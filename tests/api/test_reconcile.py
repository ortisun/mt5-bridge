"""On-demand reconciliation of an UNKNOWN order (plan §56, §114).

Seen on the demo terminal: an UNKNOWN entry blocks its symbol, and before
this route nothing but a restart could re-examine it. The route re-runs the
boot rule for one entry and resolves an entry without a ticket
to REJECTED only when the terminal holds no order or deal with this bridge's
magic in a window covering the entry; anything else stays UNKNOWN.
"""

from mt5_bridge.mt5.errors import Mt5ConnectionLost

from testkit.app_builder import build_test_app
from testkit.fixtures import WINZ25
from testkit.http_client import TypedTestClient


def _order(client_order_id: str) -> dict[str, object]:
    return {
        "client_order_id": client_order_id,
        "symbol": WINZ25,
        "side": "BUY",
        "type": "LIMIT",
        "volume": "1",
        "price": "50",
        "time_in_force": "DAY",
    }


def test_an_untraceable_unknown_order_resolves_to_rejected_and_unblocks_its_symbol() -> None:
    app, client, _state = build_test_app()
    client.queue_order_send_result(Mt5ConnectionLost("lost response"))
    with TypedTestClient(app) as http:
        lost = http.post("/v1/orders", json=_order("co-r-2"))
        assert lost.json()["state"] == "UNKNOWN"
        blocked = http.post("/v1/orders", json=_order("co-r-3"))
        assert "UNKNOWN" in blocked.json()["reason"]

        resolved = http.post("/v1/orders/co-r-2/reconcile")
        fresh = http.post("/v1/orders", json=_order("co-r-4"))
        read_back = http.get("/v1/orders/co-r-2")

    assert resolved.status_code == 200
    assert resolved.json()["state"] == "REJECTED"
    assert read_back.json()["state"] == "REJECTED"
    assert fresh.json()["state"] == "ACKNOWLEDGED"


def test_another_bridge_order_in_the_window_keeps_it_unknown() -> None:
    app, client, _state = build_test_app()
    with TypedTestClient(app) as http:
        placed = http.post("/v1/orders", json=_order("co-r-5"))
        assert placed.json()["state"] == "ACKNOWLEDGED"
        client.queue_order_send_result(Mt5ConnectionLost("lost response"))
        http.post("/v1/orders", json=_order("co-r-6"))

        kept = http.post("/v1/orders/co-r-6/reconcile")

    assert kept.status_code == 200
    assert kept.json()["state"] == "UNKNOWN"


def test_a_confirmed_order_is_not_reconcilable() -> None:
    app, _client, _state = build_test_app()
    with TypedTestClient(app) as http:
        http.post("/v1/orders", json=_order("co-r-7"))

        refused = http.post("/v1/orders/co-r-7/reconcile")

    assert refused.status_code == 409


def test_reconciling_an_unknown_client_order_id_is_not_found() -> None:
    app, _client, _state = build_test_app()
    with TypedTestClient(app) as http:
        missing = http.post("/v1/orders/nope/reconcile")

    assert missing.status_code == 404
