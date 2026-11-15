"""The MCP tools are exactly the contract's operations."""

import pytest

from mt5_bridge.mcp_adapter.contract_tools import (
    build_operations,
    load_operations,
    packaged_operations_text,
    render_operations,
    tool_name,
)

from testkit.contracts import load_openapi

_METHODS = {"get", "post", "patch", "delete", "put"}


def test_the_packaged_operations_are_generated_from_the_current_contract() -> None:
    # Stale? Run: make mcp-operations
    assert packaged_operations_text() == render_operations(load_openapi())


def test_every_contract_operation_is_a_tool() -> None:
    document = load_openapi()
    expected = {
        (tool_name(operation["operationId"]), method.upper(), path)
        for path, item in document["paths"].items()
        for method, operation in item.items()
        if method in _METHODS
    }

    version, operations = load_operations()

    assert {(o.name, o.method, o.path) for o in operations} == expected
    assert version == document["info"]["version"]


def test_tool_arguments_merge_path_query_and_body_with_references_inlined() -> None:
    _version, operations = load_operations()
    replace = next(o for o in operations if o.name == "replace_order")

    assert replace.path_params == ("client_order_id",)
    assert "new_client_order_id" in replace.body_params
    assert replace.input_schema["properties"]["volume"]["pattern"] == "^-?[0-9]+(\\.[0-9]+)?$"
    assert replace.input_schema["properties"]["side"]["enum"] == ["BUY", "SELL"]
    assert "client_order_id" in replace.input_schema["required"]
    assert "$ref" not in packaged_operations_text()


def test_descriptions_drop_the_citations_of_design_rules() -> None:
    _version, operations = load_operations()

    assert all(
        "plan §" not in o.description and "plan rule" not in o.description for o in operations
    )


def test_a_note_for_an_operation_the_contract_lacks_is_refused() -> None:
    document = load_openapi()
    del document["paths"]["/v1/heartbeat"]

    with pytest.raises(ValueError, match="heartbeat"):
        build_operations(document)


def test_an_argument_name_used_twice_is_refused() -> None:
    document = load_openapi()
    document["paths"]["/v1/orders/{client_order_id}"]["patch"]["requestBody"] = {
        "content": {
            "application/json": {
                "schema": {"type": "object", "properties": {"client_order_id": {"type": "string"}}}
            }
        }
    }

    with pytest.raises(ValueError, match="appears twice"):
        build_operations(document)
