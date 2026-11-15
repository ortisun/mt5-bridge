"""REST layer: idempotent submission, kill switch."""

from decimal import Decimal

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


class TestIdempotentSubmission:
    def test_first_submission_is_created_and_filled(self) -> None:
        app, fake_client, _state = build_test_app()
        fake_client.set_tick(
            WINZ25, bid=Decimal(100), ask=Decimal(101), last=Decimal(100), volume=Decimal(1)
        )
        with TypedTestClient(app) as http:
            response = http.post("/v1/orders", json=submit_body())

        assert response.status_code == 201
        body = response.json()
        assert body["state"] == "FILLED"
        assert body["ticket"] is not None

    def test_replaying_the_same_payload_returns_200_with_the_same_state(self) -> None:
        app, fake_client, _state = build_test_app()
        del fake_client
        with TypedTestClient(app) as http:
            first = http.post("/v1/orders", json=submit_body())
            second = http.post("/v1/orders", json=submit_body())

        assert first.status_code == 201
        assert second.status_code == 200
        assert second.json()["ticket"] == first.json()["ticket"]

    def test_a_different_payload_with_the_same_id_conflicts(self) -> None:
        app, fake_client, _state = build_test_app()
        del fake_client
        with TypedTestClient(app) as http:
            http.post("/v1/orders", json=submit_body())
            conflicting = dict(submit_body())
            conflicting["volume"] = "2"
            response = http.post("/v1/orders", json=conflicting)

        assert response.status_code == 409
        assert response.json()["code"] == "IdempotencyConflict"

    def test_getting_an_unknown_order_is_404(self) -> None:
        app, fake_client, _state = build_test_app()
        del fake_client
        with TypedTestClient(app) as http:
            response = http.get("/v1/orders/does-not-exist")

        assert response.status_code == 404
        assert response.json()["code"] == "OrderNotFound"


class TestKillSwitch:
    def test_rearm_returns_the_switch_to_normal(self) -> None:
        app, fake_client, _state = build_test_app()
        del fake_client
        with TypedTestClient(app) as http:
            halted = http.post("/v1/kill-switch", json={"action": "HALT_NEW_ORDERS"})
            rearmed = http.delete("/v1/kill-switch")

        assert halted.json()["state"] == "HALTED"
        assert rearmed.status_code == 200
        assert rearmed.json()["state"] == "NORMAL"

    def test_a_halted_switch_blocks_new_order_submission(self) -> None:
        app, fake_client, _state = build_test_app()
        del fake_client
        with TypedTestClient(app) as http:
            http.post("/v1/kill-switch", json={"action": "HALT_NEW_ORDERS"})
            response = http.post("/v1/orders", json=submit_body())

        assert response.status_code == 201  # journal accepts it, but it is rejected internally
        assert response.json()["state"] == "REJECTED"


class TestAuditLog:
    def test_a_write_is_recorded_against_the_bound_account(self) -> None:
        app, fake_client, state = build_test_app()
        del fake_client
        with TypedTestClient(app) as http:
            http.post("/v1/orders", json=submit_body())
            http.delete("/v1/kill-switch")

        submitted, rearmed = state.audit.list_no_transaction()
        assert (submitted.method, submitted.path) == ("POST", "/v1/orders")
        assert submitted.account_id == state.expected_account_id
        assert submitted.payload_hash is not None
        assert (rearmed.method, rearmed.path, rearmed.outcome) == (
            "DELETE",
            "/v1/kill-switch",
            "NORMAL",
        )
