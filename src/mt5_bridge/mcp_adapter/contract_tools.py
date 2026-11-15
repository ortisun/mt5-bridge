"""Derives the MCP tools from the REST contract.

``build_operations`` is a pure function of the parsed OpenAPI document. Its
output is committed as ``operations.json`` beside this module, so the installed
adapter needs neither the contract file nor a YAML parser at run time.
``tools/generate_mcp_operations.py`` rewrites that file and a test fails when
it is stale.
"""

import copy
import json
import re
from dataclasses import dataclass
from importlib import resources
from typing import Any, cast

_HTTP_METHODS = ("get", "post", "patch", "delete", "put")

# The contract cites design rules by number (docs/design-rules.md); an agent has no use for them.
_CITATION_IN_PARENS = re.compile(r"\s*\((?:plan|ADR|rule|audit)\b[^)]*\)")
_CITATION_INLINE = re.compile(r"\s*—\s*plan §\d+")

# Guidance an agent needs and the contract's terse summaries do not carry.
AGENT_NOTES: dict[str, str] = {
    "getHealth": "Call this first: it tells whether the terminal is connected to the broker.",
    "getSymbol": (
        "Read this before building an order: prices must be multiples of tick_size and "
        "volumes multiples of volume_step within volume_min and volume_max."
    ),
    "getBars": "The bar still forming is never returned.",
    "listOpenOrders": "This is the bridge's own journal; an order sent outside the bridge is not here.",
    "submitOrder": (
        "Sends a real order to the broker account. Choose a fresh, unique client_order_id; "
        "repeating the call with the same id and the same payload returns the first order "
        "and never sends a second one. A state of UNKNOWN means the bridge could not "
        "confirm the outcome: do not resubmit under a new id, read the order with get_order "
        "and tell the operator. price is required for LIMIT, STOP and STOP_LIMIT orders."
    ),
    "checkOrder": "Asks the terminal whether the order would be accepted. Nothing is sent to the market.",
    "amendOrder": "Send only the fields that change.",
    "cancelOrder": "The returned state can be FILLED when the cancellation lost the race.",
    "replaceOrder": (
        "Choose a fresh operation_id; repeating the call with the same operation_id returns "
        "the stored outcome. Read the outcome field: both orders can end open, or neither."
    ),
    "reconcileOrder": "For an operator resolving an order stuck in UNKNOWN.",
    "listHistoryOrders": "from_epoch and to_epoch bound the search window.",
    "listHistoryDeals": "from_epoch and to_epoch bound the search window.",
    "activateKillSwitch": (
        "Emergency stop, and it only escalates: HALT_NEW_ORDERS blocks new orders, "
        "CANCEL_ALL also cancels every open order, FLATTEN also closes every position at "
        "market. Only rearm_kill_switch returns it to NORMAL."
    ),
    "rearmKillSwitch": (
        "Orders are accepted again afterwards. An operator's decision: do not call it "
        "unless the user asked for exactly this."
    ),
    "heartbeat": (
        "The first heartbeat arms the dead-man switch: from then on, open orders are "
        "canceled when heartbeats stop for longer than the bridge's timeout. Do not call "
        "it unless you will keep calling it."
    ),
}


@dataclass(frozen=True, slots=True)
class Operation:
    """One contract operation, shaped as an MCP tool."""

    name: str
    operation_id: str
    method: str
    path: str
    description: str
    input_schema: dict[str, Any]
    path_params: tuple[str, ...]
    query_params: tuple[str, ...]
    body_params: tuple[str, ...]


def tool_name(operation_id: str) -> str:
    """``listOpenOrders`` -> ``list_open_orders``."""
    return re.sub(r"(?<!^)(?=[A-Z])", "_", operation_id).lower()


def _resolve(node: Any, document: dict[str, Any]) -> Any:
    """A deep copy of ``node`` with every local ``$ref`` replaced by its target."""
    if isinstance(node, dict):
        mapping = cast("dict[str, Any]", node)
        ref = mapping.get("$ref")
        if isinstance(ref, str):
            target: Any = document
            for part in ref.removeprefix("#/").split("/"):
                target = target[part]
            return _resolve(target, document)
        return {key: _resolve(value, document) for key, value in mapping.items()}
    if isinstance(node, list):
        return [_resolve(item, document) for item in cast("list[Any]", node)]
    return copy.deepcopy(node)


def _clean(text: str) -> str:
    text = _CITATION_IN_PARENS.sub("", text)
    text = _CITATION_INLINE.sub("", text)
    return " ".join(text.split())


def _description(operation: dict[str, Any]) -> str:
    parts = [
        _clean(str(operation[key])) for key in ("summary", "description") if operation.get(key)
    ]
    note = AGENT_NOTES.get(str(operation["operationId"]))
    if note:
        parts.append(note)
    return " ".join(parts)


def _build_operation(
    document: dict[str, Any], path: str, method: str, raw: dict[str, Any]
) -> dict[str, Any]:
    operation_id = str(raw["operationId"])

    properties: dict[str, Any] = {}
    required: list[str] = []
    params: dict[str, list[str]] = {"path": [], "query": [], "body": []}

    def add(name: str, schema: dict[str, Any], *, where: str, is_required: bool) -> None:
        if name in properties:
            raise ValueError(f"{operation_id}: argument {name!r} appears twice")
        properties[name] = schema
        params[where].append(name)
        if is_required:
            required.append(name)

    parameters: list[dict[str, Any]] = _resolve(raw.get("parameters", []), document)
    for parameter in parameters:
        where = str(parameter["in"])
        if where not in ("path", "query"):
            raise ValueError(f"{operation_id}: unsupported parameter location {where!r}")
        schema: dict[str, Any] = dict(parameter["schema"])
        if parameter.get("description"):
            schema["description"] = _clean(str(parameter["description"]))
        add(
            str(parameter["name"]),
            schema,
            where=where,
            is_required=bool(parameter.get("required", False)),
        )

    body = raw.get("requestBody")
    if body is not None:
        body_schema: dict[str, Any] = _resolve(
            body["content"]["application/json"]["schema"], document
        )
        if body_schema.get("type") != "object":
            raise ValueError(f"{operation_id}: the request body must be an object")
        body_required: set[str] = set(body_schema.get("required", []))
        body_properties: dict[str, dict[str, Any]] = body_schema.get("properties", {})
        for name, property_schema in body_properties.items():
            if "description" in property_schema:
                property_schema["description"] = _clean(str(property_schema["description"]))
            add(name, property_schema, where="body", is_required=name in body_required)

    return {
        "name": tool_name(operation_id),
        "operation_id": operation_id,
        "method": method.upper(),
        "path": path,
        "description": _description(raw),
        "input_schema": {
            "type": "object",
            "properties": properties,
            "required": required,
            "additionalProperties": False,
        },
        "path_params": params["path"],
        "query_params": params["query"],
        "body_params": params["body"],
    }


def build_operations(document: dict[str, Any]) -> dict[str, Any]:
    """The content of ``operations.json`` for one parsed OpenAPI document."""
    paths: dict[str, dict[str, Any]] = document["paths"]
    operations = [
        _build_operation(document, path, method, item[method])
        for path, item in paths.items()
        for method in _HTTP_METHODS
        if method in item
    ]
    known = {str(operation["operation_id"]) for operation in operations}
    orphans = set(AGENT_NOTES) - known
    if orphans:
        raise ValueError(f"AGENT_NOTES names operations the contract lacks: {sorted(orphans)}")
    return {"contract_version": str(document["info"]["version"]), "operations": operations}


def render_operations(document: dict[str, Any]) -> str:
    """``build_operations`` as the exact text of ``operations.json``."""
    return json.dumps(build_operations(document), indent=2, ensure_ascii=False) + "\n"


def packaged_operations_text() -> str:
    return (
        resources.files("mt5_bridge.mcp_adapter")
        .joinpath("operations.json")
        .read_text(encoding="utf-8")
    )


def load_operations() -> tuple[str, tuple[Operation, ...]]:
    """The contract version and the operations packaged with the adapter."""
    data: dict[str, Any] = json.loads(packaged_operations_text())
    items: list[dict[str, Any]] = data["operations"]
    operations = tuple(
        Operation(
            name=item["name"],
            operation_id=item["operation_id"],
            method=item["method"],
            path=item["path"],
            description=item["description"],
            input_schema=item["input_schema"],
            path_params=tuple(item["path_params"]),
            query_params=tuple(item["query_params"]),
            body_params=tuple(item["body_params"]),
        )
        for item in items
    )
    return str(data["contract_version"]), operations
