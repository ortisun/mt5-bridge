"""Problem Details (RFC 9457) and the HTTP semantics that go with each status."""

from pathlib import Path

import pytest

from mt5_bridge.api.errors import (
    ABOUT_BLANK,
    CATALOGUE,
    PROBLEM_TYPE_PREFIX,
    validation_errors,
)
from mt5_bridge.mt5.fake_client import FakeMt5Client

from testkit.app_builder import build_test_app
from testkit.fixtures import WINZ25
from testkit.http_client import TypedTestClient

ERRORS_DOC = Path(__file__).resolve().parents[2] / "docs" / "errors.md"


def limit_order(client_order_id: str) -> dict[str, object]:
    return {
        "client_order_id": client_order_id,
        "symbol": WINZ25,
        "side": "BUY",
        "type": "LIMIT",
        "price": "50",
        "volume": "1",
        "time_in_force": "DAY",
    }


class TestCatalogue:
    def test_every_type_has_its_own_uri_and_code(self) -> None:
        assert len({problem.uri for problem in CATALOGUE}) == len(CATALOGUE)
        assert len({problem.code for problem in CATALOGUE}) == len(CATALOGUE)
        assert all(problem.uri.startswith(PROBLEM_TYPE_PREFIX) for problem in CATALOGUE)

    def test_only_a_retryable_type_tells_the_client_when_to_retry(self) -> None:
        for problem in CATALOGUE:
            assert (problem.retry_after_seconds is not None) == problem.retryable, problem.slug

    def test_the_documentation_lists_every_type_with_its_status_and_code(self) -> None:
        documented = ERRORS_DOC.read_text(encoding="utf-8")

        for problem in CATALOGUE:
            row = f"| `{problem.uri}` | {problem.status} | `{problem.code}` |"
            assert row in documented, f"docs/errors.md lacks the row {row}"


class TestProblemMembers:
    def test_a_catalogue_problem_carries_the_standard_and_the_extension_members(self) -> None:
        app, _fake_client, _state = build_test_app()
        with TypedTestClient(app) as http:
            response = http.get("/v1/orders/nope")

        assert response.json() == {
            "type": "urn:mt5-bridge:problem:order-not-found",
            "title": "Unknown order",
            "status": 404,
            "detail": "unknown client_order_id 'nope'",
            "instance": "/v1/orders/nope",
            "code": "OrderNotFound",
            "retryable": False,
            "mt5_retcode": None,
            "mt5_last_error": None,
        }

    def test_the_instance_is_the_path_without_the_query_string(self) -> None:
        app, _fake_client, _state = build_test_app()
        with TypedTestClient(app) as http:
            response = http.get("/v1/bars", params={"symbol": WINZ25})

        assert response.json()["instance"] == "/v1/bars"

    def test_a_method_the_path_does_not_allow_keeps_its_allow_header(self) -> None:
        app, _fake_client, _state = build_test_app()
        with TypedTestClient(app) as http:
            response = http.delete("/v1/health")

        assert response.status_code == 405
        assert response.headers["allow"] == "GET"
        assert response.json()["type"] == ABOUT_BLANK
        assert response.json()["title"] == "Method Not Allowed"


class TestValidation:
    def test_a_body_fault_is_located_by_a_json_pointer(self) -> None:
        app, _fake_client, _state = build_test_app()
        invalid = limit_order("co-1")
        del invalid["volume"]
        invalid["side"] = "HOLD"
        with TypedTestClient(app) as http:
            response = http.post("/v1/orders", json=invalid)

        assert response.status_code == 422
        body = response.json()
        assert body["code"] == "ValidationError"
        assert {error["pointer"] for error in body["errors"]} == {"#/volume", "#/side"}
        assert all(set(error) == {"pointer", "detail"} for error in body["errors"])

    def test_a_query_fault_is_located_by_its_parameter(self) -> None:
        app, _fake_client, _state = build_test_app()
        with TypedTestClient(app) as http:
            response = http.get(
                "/v1/bars",
                params={"symbol": WINZ25, "timeframe": "M7"},
            )

        assert response.status_code == 422
        assert [(e["in"], e["parameter"]) for e in response.json()["errors"]] == [
            ("query", "timeframe")
        ]

    def test_the_rejected_input_is_not_copied_into_the_problem(self) -> None:
        app, _fake_client, _state = build_test_app()
        invalid = limit_order("co-1")
        invalid["side"] = {"password": "hunter2"}
        with TypedTestClient(app) as http:
            response = http.post("/v1/orders", json=invalid)

        assert response.status_code == 422
        assert "hunter2" not in response.text

    def test_a_bare_json_number_for_a_decimal_is_a_422_not_a_500(self) -> None:
        app, _fake_client, _state = build_test_app()
        invalid = limit_order("co-1")
        invalid["volume"] = 1
        with TypedTestClient(app) as http:
            response = http.post("/v1/orders", json=invalid)

        assert response.status_code == 422
        assert response.json()["errors"][0]["pointer"] == "#/volume"

    def test_an_unknown_kill_switch_action_names_the_member(self) -> None:
        app, _fake_client, _state = build_test_app()
        with TypedTestClient(app) as http:
            response = http.post("/v1/kill-switch", json={"action": "EXPLODE"})

        assert response.status_code == 422
        assert response.json()["errors"][0]["pointer"] == "#/action"

    def test_a_pointer_escapes_the_characters_rfc_6901_reserves(self) -> None:
        located = validation_errors([{"loc": ("body", "a/b", "c~d", 0), "msg": "bad"}])

        assert located == [{"pointer": "#/a~1b/c~0d/0", "detail": "bad"}]

    def test_malformed_json_points_at_the_whole_body(self) -> None:
        app, _fake_client, _state = build_test_app()
        headers = {"content-type": "application/json"}
        with TypedTestClient(app) as http:
            response = http.post("/v1/orders", content=b"{not json", headers=headers)

        assert response.status_code == 422
        assert response.json()["errors"][0]["pointer"] == "#"


class TestStateConflict:
    def test_amending_an_order_that_is_not_resting_is_a_409_state_conflict(self) -> None:
        app, _fake_client, _state = build_test_app()
        market = {**limit_order("co-1"), "type": "MARKET"}
        del market["price"]
        with TypedTestClient(app) as http:
            http.post("/v1/orders", json=market)
            response = http.patch("/v1/orders/co-1", json={"price": "51"})

        assert response.status_code == 409
        assert response.json()["code"] == "OrderStateConflict"
        assert response.json()["type"] == "urn:mt5-bridge:problem:order-state-conflict"


class TestUnavailable:
    def test_a_disconnected_terminal_is_a_retryable_503_with_retry_after(self) -> None:
        app, fake_client, _state = build_test_app()
        fake_client.simulate_disconnect()
        with TypedTestClient(app) as http:
            response = http.get("/v1/account")

        assert response.status_code == 503
        assert response.headers["retry-after"] == "5"
        assert response.json()["code"] == "TerminalUnavailable"
        assert response.json()["retryable"] is True

    def test_a_stopped_worker_on_a_read_is_a_retryable_503_with_retry_after(self) -> None:
        app, _fake_client, state = build_test_app()
        with TypedTestClient(app) as http:
            state.worker.stop()
            response = http.get("/v1/account")

        assert response.status_code == 503
        assert response.headers["retry-after"] == "1"
        assert response.json()["code"] == "WorkerUnavailable"

    def test_a_replace_under_clock_drift_is_a_retryable_503_not_a_500(self) -> None:
        app, _fake_client, state = build_test_app()
        with TypedTestClient(app) as http:
            http.post("/v1/orders", json=limit_order("co-1"))
            state.clock_drift_state.drift_seconds = 3600.0
            replacement = {
                **limit_order("co-2"),
                "operation_id": "op-1",
                "new_client_order_id": "co-2",
            }
            del replacement["client_order_id"]
            response = http.post("/v1/orders/co-1/replace", json=replacement)

        assert response.status_code == 503
        assert response.headers["retry-after"] == "60"
        assert response.json()["code"] == "ClockDriftExceeded"
        assert response.json()["retryable"] is True


class TestUnexpectedError:
    def test_an_unhandled_exception_is_a_500_problem_that_quotes_nothing(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        app, _fake_client, _state = build_test_app()

        def fail(self: FakeMt5Client) -> None:
            raise RuntimeError("password=hunter2 at /srv/bridge/secret.py")

        monkeypatch.setattr(FakeMt5Client, "terminal_info", fail)
        with TypedTestClient(app, raise_server_exceptions=False) as http:
            response = http.get("/v1/health")

        assert response.status_code == 500
        assert response.headers["content-type"] == "application/problem+json"
        body = response.json()
        assert body["type"] == "urn:mt5-bridge:problem:internal-error"
        assert body["code"] == "InternalError"
        assert body["retryable"] is False
        assert "hunter2" not in response.text
        assert "secret.py" not in response.text
        assert "Traceback" not in response.text
