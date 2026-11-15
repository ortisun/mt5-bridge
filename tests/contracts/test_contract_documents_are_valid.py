"""The contract files themselves are well-formed (independent of any server)."""

from jsonschema import Draft202012Validator

from testkit.contracts import load_openapi, load_ws_schema

REQUIRED_REST_ROUTES = {
    ("get", "/v1/health"),
    ("get", "/v1/terminal"),
    ("get", "/v1/account"),
    ("get", "/v1/symbols"),
    ("get", "/v1/symbols/{symbol}"),
    ("get", "/v1/bars"),
    ("get", "/v1/ticks"),
    ("get", "/v1/book/{symbol}"),
    ("get", "/v1/orders"),
    ("get", "/v1/orders/{client_order_id}"),
    ("get", "/v1/positions"),
    ("get", "/v1/history/orders"),
    ("get", "/v1/history/deals"),
    ("post", "/v1/orders/check"),
    ("post", "/v1/orders"),
    ("patch", "/v1/orders/{client_order_id}"),
    ("post", "/v1/orders/{client_order_id}/replace"),
    ("delete", "/v1/orders/{client_order_id}"),
    ("post", "/v1/kill-switch"),
    ("delete", "/v1/kill-switch"),
    ("post", "/v1/heartbeat"),
}


class TestOpenapiDocument:
    def test_declares_openapi_3_1(self) -> None:
        document = load_openapi()

        assert document["openapi"].startswith("3.1")

    def test_every_required_bridge_route_is_present(self) -> None:
        document = load_openapi()

        present = {
            (method, path)
            for path, operations in document["paths"].items()
            for method in operations
            if method in {"get", "post", "patch", "delete", "put"}
        }

        missing = REQUIRED_REST_ROUTES - present
        assert missing == set()

    def test_the_error_envelope_has_the_required_fields(self) -> None:
        document = load_openapi()

        error = document["components"]["schemas"]["Error"]
        assert set(error["required"]) == {"code", "message", "retryable"}
        assert set(error["properties"]) == {
            "code",
            "message",
            "mt5_retcode",
            "mt5_last_error",
            "retryable",
        }

    def test_replace_order_result_is_a_discriminated_union(self) -> None:
        document = load_openapi()

        result = document["components"]["schemas"]["ReplaceOrderResult"]
        assert "discriminator" in result
        assert result["discriminator"]["propertyName"] == "outcome"
        mapped_outcomes = set(result["discriminator"]["mapping"])
        assert mapped_outcomes == {
            "REPLACED",
            "CANCEL_FAILED",
            "CANCELED_NEW_REJECTED",
            "BOTH_OPEN",
            "AMENDED",
            "UNKNOWN",
        }


class TestWsMessagesSchema:
    def test_is_a_valid_json_schema_2020_12_document(self) -> None:
        schema = load_ws_schema()

        Draft202012Validator.check_schema(schema)

    def test_declares_every_required_client_op(self) -> None:
        schema = load_ws_schema()

        defs = schema["$defs"]
        ops = {
            defs["SubscribeMessage"]["properties"]["op"]["const"],
            defs["UnsubscribeMessage"]["properties"]["op"]["const"],
            defs["ResumeMessage"]["properties"]["op"]["const"],
            defs["PingMessage"]["properties"]["op"]["const"],
        }
        assert ops == {"subscribe", "unsubscribe", "resume", "ping"}

    def test_the_envelope_carries_every_required_field(self) -> None:
        schema = load_ws_schema()

        envelope = schema["$defs"]["StreamEnvelope"]
        assert set(envelope["required"]) == {
            "channel",
            "stream_id",
            "seq",
            "ts_utc",
            "source_time_raw",
            "source_time_unit",
            "time_basis",
            "received_at",
            "payload",
        }

    def test_declares_resync_required(self) -> None:
        schema = load_ws_schema()

        resync = schema["$defs"]["ResyncRequiredMessage"]
        assert resync["properties"]["type"]["const"] == "RESYNC_REQUIRED"

    def test_the_book_channel_has_a_discriminated_payload_schema(self) -> None:
        schema = load_ws_schema()

        envelope = schema["$defs"]["StreamEnvelope"]
        book_rule = next(
            rule
            for rule in envelope["allOf"]
            if rule["if"]["properties"]["channel"]["const"] == "book"
        )
        assert book_rule["then"]["properties"]["payload"]["$ref"] == "#/$defs/BookPayload"

        book_payload = schema["$defs"]["BookPayload"]
        assert set(book_payload["required"]) == {"symbol", "levels"}
        assert book_payload["properties"]["levels"]["items"]["$ref"] == "#/$defs/BookLevel"
