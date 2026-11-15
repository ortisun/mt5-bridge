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

    def test_the_problem_schema_has_the_rfc_9457_members_and_the_bridge_extensions(self) -> None:
        document = load_openapi()

        problem = document["components"]["schemas"]["Problem"]
        assert set(problem["required"]) == {
            "type",
            "title",
            "status",
            "detail",
            "code",
            "retryable",
        }
        assert set(problem["properties"]) == {
            "type",
            "title",
            "status",
            "detail",
            "instance",
            "code",
            "retryable",
            "mt5_retcode",
            "mt5_last_error",
            "errors",
        }

    def test_every_error_response_is_a_problem_document(self) -> None:
        document = load_openapi()
        problem = {"$ref": "#/components/schemas/Problem"}

        checked = 0
        for path, operations in document["paths"].items():
            for method, operation in operations.items():
                if method not in {"get", "post", "patch", "delete", "put"}:
                    continue
                for status, response in operation["responses"].items():
                    if not status.startswith(("4", "5")):
                        continue
                    if "$ref" in response:
                        name = response["$ref"].rsplit("/", 1)[1]
                        response = document["components"]["responses"][name]
                    where = f"{method} {path} {status}"
                    assert set(response["content"]) == {"application/problem+json"}, where
                    assert response["content"]["application/problem+json"]["schema"] == problem
                    checked += 1
        assert checked > 0

    def test_every_operation_declares_the_errors_any_request_can_get(self) -> None:
        document = load_openapi()

        for path, operations in document["paths"].items():
            for method, operation in operations.items():
                if method not in {"get", "post", "patch", "delete", "put"}:
                    continue
                missing = {"500", "503"} - set(operation["responses"])
                assert missing == set(), f"{method} {path} lacks {missing}"

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

    def test_a_data_event_requires_the_ocsf_base_attributes(self) -> None:
        schema = load_ws_schema()

        envelope = schema["$defs"]["StreamEnvelope"]
        assert set(envelope["required"]) == {
            "class_uid",
            "category_uid",
            "activity_id",
            "type_uid",
            "time",
            "severity_id",
            "metadata",
            "unmapped",
        }
        assert envelope["additionalProperties"] is False

    def test_the_metadata_carries_the_sequencing_and_time_fields(self) -> None:
        schema = load_ws_schema()

        metadata = schema["$defs"]["StreamMetadata"]
        assert set(metadata["required"]) == {
            "version",
            "product",
            "event_code",
            "log_name",
            "sequence",
            "original_time",
            "processed_time",
        }
        assert metadata["properties"]["version"]["const"] == "1.9.0"
        unmapped = schema["$defs"]["StreamUnmapped"]
        assert set(unmapped["required"]) == {"source_time_unit", "time_basis"}

    def test_declares_resync_required(self) -> None:
        schema = load_ws_schema()

        resync = schema["$defs"]["ResyncRequiredMessage"]
        assert resync["properties"]["status_code"]["const"] == "RESYNC_REQUIRED"
        assert (
            resync["properties"]["metadata"]["properties"]["event_code"]["const"]
            == "RESYNC_REQUIRED"
        )

    def test_the_book_channel_has_a_discriminated_payload_schema(self) -> None:
        schema = load_ws_schema()

        envelope = schema["$defs"]["StreamEnvelope"]
        book_rule = next(
            rule
            for rule in envelope["allOf"]
            if rule["if"]["properties"]["metadata"]["properties"]["event_code"].get("const")
            == "book"
        )
        payload = book_rule["then"]["properties"]["unmapped"]["properties"]["payload"]
        assert payload["$ref"] == "#/$defs/BookPayload"

        book_payload = schema["$defs"]["BookPayload"]
        assert set(book_payload["required"]) == {"symbol", "levels"}
        assert book_payload["properties"]["levels"]["items"]["$ref"] == "#/$defs/BookLevel"
