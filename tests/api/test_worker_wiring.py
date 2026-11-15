"""End-to-end proof that worker admission/shutdown are handled correctly by
the running app: a saturated MARKET_POLL backlog never touches an order, and
a worker that becomes unavailable mid-flight finalizes deterministically
instead of surfacing an unmapped 500 (plan §114 review follow-up).
"""

from mt5_bridge.core.worker import CommandPriority

from testkit.app_builder import build_test_app
from testkit.fixtures import WINZ25
from testkit.http_client import TypedTestClient


def limit_order_body(client_order_id: str) -> dict[str, object]:
    return {
        "client_order_id": client_order_id,
        "symbol": WINZ25,
        "side": "BUY",
        "type": "LIMIT",
        "price": "50",
        "volume": "1",
        "time_in_force": "DAY",
    }


class TestMarketPollSaturationNeverAffectsOrders:
    def test_a_saturated_market_poll_backlog_still_lets_an_order_through(self) -> None:
        app, _fake_client, state = build_test_app(max_market_poll_backlog=1)
        # Fill the one MARKET_POLL slot and never drain it (no real thread
        # picks it up before the assertion below runs, since it is far
        # cheaper than the HTTP round trip that follows).
        state.worker.submit(CommandPriority.MARKET_POLL, lambda c: c.terminal_info())

        with TypedTestClient(app) as http:
            response = http.post("/v1/orders", json=limit_order_body("co-satmp-1"))

        assert response.status_code == 201
        assert response.json()["state"] == "ACKNOWLEDGED"


class TestWorkerUnavailableFinalizesDeterministically:
    def test_a_stopped_worker_rejects_a_new_submission_instead_of_500(self) -> None:
        app, _fake_client, state = build_test_app()
        state.worker.stop()

        with TypedTestClient(app) as http:
            response = http.post("/v1/orders", json=limit_order_body("co-stopped-1"))

        assert response.status_code == 201  # a finalized journal row, not an unhandled error
        body = response.json()
        assert body["state"] == "REJECTED"
        assert "worker unavailable" in body["reason"]

        with TypedTestClient(app) as http:
            fetched = http.get("/v1/orders/co-stopped-1")
        assert fetched.json()["state"] == "REJECTED"  # the RECEIVED row was not left dangling

    def test_a_stopped_worker_maps_a_cancel_to_a_retryable_503_not_500(self) -> None:
        app, _fake_client, state = build_test_app()
        with TypedTestClient(app) as http:
            submitted = http.post("/v1/orders", json=limit_order_body("co-stopped-2"))
            assert submitted.json()["state"] == "ACKNOWLEDGED"

            state.worker.stop()
            response = http.delete("/v1/orders/co-stopped-2")

        assert response.status_code == 503
        body = response.json()
        assert body["code"] == "WorkerUnavailable"
        assert body["retryable"] is True

        # The original order's own state is untouched — safe to retry.
        with TypedTestClient(app) as http:
            fetched = http.get("/v1/orders/co-stopped-2")
        assert fetched.json()["state"] == "ACKNOWLEDGED"
