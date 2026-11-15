"""Real REST responses, from the running app, validated against openapi.yaml."""

from decimal import Decimal
from typing import Any

import pytest

from mt5_bridge.mt5.fake_client import FakeMt5Client

from testkit.app_builder import build_test_app
from testkit.contracts import assert_conforms, load_openapi, openapi_schema_validator
from testkit.fixtures import PETR4, WINZ25
from testkit.http_client import Response, TypedTestClient

OPENAPI = load_openapi()


def submit_body(
    client_order_id: str, *, symbol: str = WINZ25, volume: str = "1"
) -> dict[str, object]:
    return {
        "client_order_id": client_order_id,
        "symbol": symbol,
        "side": "BUY",
        "type": "MARKET",
        "volume": volume,
        "time_in_force": "DAY",
    }


def resting_body(client_order_id: str, *, price: str = "50") -> dict[str, object]:
    return {
        "client_order_id": client_order_id,
        "symbol": WINZ25,
        "side": "BUY",
        "type": "LIMIT",
        "price": price,
        "volume": "1",
        "time_in_force": "DAY",
    }


class TestHealthConforms:
    def test_health_response_matches_its_schema(self) -> None:
        app, fake_client, _state = build_test_app()
        del fake_client
        validator = openapi_schema_validator(OPENAPI, "HealthResponse")
        with TypedTestClient(app) as http:
            response = http.get("/v1/health")

        assert_conforms(validator, response.json())


class TestOrderConforms:
    def test_a_filled_market_order_matches_the_order_schema(self) -> None:
        app, fake_client, _state = build_test_app()
        del fake_client
        validator = openapi_schema_validator(OPENAPI, "Order")
        with TypedTestClient(app) as http:
            response = http.post("/v1/orders", json=submit_body("co-1"))

        assert_conforms(validator, response.json())

    def test_a_rejected_order_matches_the_order_schema(self) -> None:
        app, fake_client, _state = build_test_app()
        del fake_client
        validator = openapi_schema_validator(OPENAPI, "Order")
        with TypedTestClient(app) as http:
            response = http.post(
                "/v1/orders",
                json=submit_body("co-2", symbol=PETR4, volume="1"),  # below PETR4's min volume
            )

        assert response.json()["state"] == "REJECTED"
        assert_conforms(validator, response.json())


def assert_is_a_problem(response: Response, status: int) -> dict[str, Any]:
    """The response is a Problem Details document that matches the contract."""
    assert response.status_code == status
    assert response.headers["content-type"] == "application/problem+json"
    body: dict[str, Any] = response.json()
    assert_conforms(openapi_schema_validator(OPENAPI, "Problem"), body)
    assert body["status"] == status
    return body


class TestProblemConforms:
    def test_a_404_body_is_a_problem(self) -> None:
        app, _fake_client, _state = build_test_app()
        with TypedTestClient(app) as http:
            response = http.get("/v1/orders/does-not-exist")

        assert_is_a_problem(response, 404)

    def test_an_unknown_path_is_an_about_blank_problem(self) -> None:
        app, _fake_client, _state = build_test_app()
        with TypedTestClient(app) as http:
            response = http.get("/v1/no-such-route")

        assert assert_is_a_problem(response, 404)["type"] == "about:blank"

    def test_a_405_body_is_a_problem(self) -> None:
        app, _fake_client, _state = build_test_app()
        with TypedTestClient(app) as http:
            response = http.delete("/v1/health")

        assert_is_a_problem(response, 405)

    def test_a_409_conflict_body_is_a_problem(self) -> None:
        app, _fake_client, _state = build_test_app()
        with TypedTestClient(app) as http:
            http.post("/v1/orders", json=submit_body("co-3"))
            conflicting = dict(submit_body("co-3"))
            conflicting["volume"] = "2"
            response = http.post("/v1/orders", json=conflicting)

        assert_is_a_problem(response, 409)

    def test_a_413_body_is_a_problem(self) -> None:
        app, _fake_client, state = build_test_app()
        oversized = submit_body("x" * (state.settings.max_request_body_bytes + 1))
        with TypedTestClient(app) as http:
            response = http.post("/v1/orders", json=oversized)

        assert_is_a_problem(response, 413)

    def test_a_422_body_is_a_problem_with_located_errors(self) -> None:
        app, _fake_client, _state = build_test_app()
        invalid = dict(submit_body("co-4"))
        del invalid["volume"]
        with TypedTestClient(app) as http:
            response = http.post("/v1/orders", json=invalid)

        body = assert_is_a_problem(response, 422)
        assert body["errors"] != []

    def test_a_500_body_is_a_problem(self, monkeypatch: pytest.MonkeyPatch) -> None:
        app, _fake_client, _state = build_test_app()

        def fail(self: FakeMt5Client) -> None:
            raise RuntimeError("unexpected")

        monkeypatch.setattr(FakeMt5Client, "terminal_info", fail)
        with TypedTestClient(app, raise_server_exceptions=False) as http:
            response = http.get("/v1/health")

        assert_is_a_problem(response, 500)

    def test_a_503_body_is_a_problem(self) -> None:
        app, fake_client, _state = build_test_app()
        fake_client.simulate_disconnect()
        with TypedTestClient(app) as http:
            response = http.get("/v1/account")

        assert_is_a_problem(response, 503)


class TestReplaceOrderResultConforms:
    def test_replaced_outcome_matches_its_discriminated_schema(self) -> None:
        app, fake_client, _state = build_test_app()
        del fake_client
        validator = openapi_schema_validator(OPENAPI, "ReplaceOrderResult")
        with TypedTestClient(app) as http:
            http.post("/v1/orders", json=resting_body("co-orig-1"))
            response = http.post(
                "/v1/orders/co-orig-1/replace",
                json={
                    "operation_id": "op-1",
                    "new_client_order_id": "co-new-1",
                    "symbol": WINZ25,
                    "side": "BUY",
                    "type": "LIMIT",
                    "price": "60",
                    "volume": "1",
                    "time_in_force": "DAY",
                },
            )

        assert response.json()["outcome"] == "REPLACED"
        assert_conforms(validator, response.json())

    def test_cancel_failed_outcome_matches_its_discriminated_schema(self) -> None:
        app, fake_client, _state = build_test_app()
        validator = openapi_schema_validator(OPENAPI, "ReplaceOrderResult")
        with TypedTestClient(app) as http:
            submitted = http.post("/v1/orders", json=resting_body("co-orig-2")).json()
            fake_client.force_fill(submitted["ticket"])
            response = http.post(
                "/v1/orders/co-orig-2/replace",
                json={
                    "operation_id": "op-2",
                    "new_client_order_id": "co-new-2",
                    "symbol": WINZ25,
                    "side": "BUY",
                    "type": "LIMIT",
                    "price": "60",
                    "volume": "1",
                    "time_in_force": "DAY",
                },
            )

        assert response.json()["outcome"] == "CANCEL_FAILED"
        assert response.json()["new"] is None
        assert_conforms(validator, response.json())


class TestTicksConforms:
    def test_the_latest_tick_matches_the_tick_schema(self) -> None:
        app, fake_client, _state = build_test_app()
        fake_client.set_tick(
            WINZ25, bid=Decimal(100), ask=Decimal(101), last=Decimal(100), volume=Decimal(1)
        )
        validator = openapi_schema_validator(OPENAPI, "Tick")
        with TypedTestClient(app) as http:
            response = http.get("/v1/ticks/latest", params={"symbol": WINZ25})

        body = response.json()
        assert len(body) == 1
        assert_conforms(validator, body[0])


class TestKillSwitchConforms:
    def test_kill_switch_status_matches_its_schema(self) -> None:
        app, fake_client, _state = build_test_app()
        del fake_client
        validator = openapi_schema_validator(OPENAPI, "KillSwitchStatus")
        with TypedTestClient(app) as http:
            response = http.delete("/v1/kill-switch")

        assert_conforms(validator, response.json())
