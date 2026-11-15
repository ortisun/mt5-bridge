"""Real REST responses, from the running app, validated against openapi.yaml."""

from decimal import Decimal

from testkit.app_builder import build_test_app
from testkit.contracts import assert_conforms, load_openapi, openapi_schema_validator
from testkit.fixtures import PETR4, WINZ25
from testkit.http_client import TypedTestClient

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


class TestErrorEnvelopeConforms:
    def test_a_404_body_matches_the_error_schema(self) -> None:
        app, fake_client, _state = build_test_app()
        del fake_client
        validator = openapi_schema_validator(OPENAPI, "Error")
        with TypedTestClient(app) as http:
            response = http.get("/v1/orders/does-not-exist")

        assert response.status_code == 404
        assert_conforms(validator, response.json())

    def test_a_409_conflict_body_matches_the_error_schema(self) -> None:
        app, fake_client, _state = build_test_app()
        del fake_client
        validator = openapi_schema_validator(OPENAPI, "Error")
        with TypedTestClient(app) as http:
            http.post("/v1/orders", json=submit_body("co-3"))
            conflicting = dict(submit_body("co-3"))
            conflicting["volume"] = "2"
            response = http.post("/v1/orders", json=conflicting)

        assert response.status_code == 409
        assert_conforms(validator, response.json())


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
    def test_a_default_window_tick_matches_the_tick_schema(self) -> None:
        app, fake_client, _state = build_test_app()
        fake_client.set_tick(
            WINZ25, bid=Decimal(100), ask=Decimal(101), last=Decimal(100), volume=Decimal(1)
        )
        validator = openapi_schema_validator(OPENAPI, "Tick")
        with TypedTestClient(app) as http:
            response = http.get("/v1/ticks", params={"symbol": WINZ25})

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
