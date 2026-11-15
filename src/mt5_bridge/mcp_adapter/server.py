"""The MCP server: one tool per contract operation."""

import json
from importlib import resources
from typing import Any

import httpx2
from mcp import types
from mcp.server.context import ServerRequestContext
from mcp.server.lowlevel import Server
from starlette.applications import Starlette

from mt5_bridge.mcp_adapter.bridge_client import MissingArgumentError, call_operation
from mt5_bridge.mcp_adapter.config import AdapterConfig
from mt5_bridge.mcp_adapter.contract_tools import Operation, load_operations

GUIDE_URI = "mt5-bridge://guide"

_INSTRUCTIONS = (
    "Tools for one MetaTrader 5 terminal and broker account, through the mt5-bridge "
    "gateway. Prices, volumes and money are decimal strings; pass them back exactly. "
    "Call get_health first. Trading tools act on a real account: confirm with the user "
    "before using them. A failed call returns a problem document; retry only when its "
    f"retryable member is true. Read the resource {GUIDE_URI} before trading."
)


def _annotations(operation: Operation) -> types.ToolAnnotations:
    read_only = operation.method == "GET"
    return types.ToolAnnotations(
        read_only_hint=read_only,
        destructive_hint=not read_only and operation.operation_id != "checkOrder",
        idempotent_hint=read_only or operation.operation_id in {"submitOrder", "replaceOrder"},
        open_world_hint=True,
    )


def _text(payload: Any) -> list[types.ContentBlock]:
    text = payload if isinstance(payload, str) else json.dumps(payload, ensure_ascii=False)
    return [types.TextContent(text=text)]


def _error(message: str) -> types.CallToolResult:
    return types.CallToolResult(content=_text(message), is_error=True)


def build_server(config: AdapterConfig, http: httpx2.AsyncClient) -> Server[Any]:
    """``http`` must have the bridge address as its base URL."""
    contract_version, operations = load_operations()
    by_name = {operation.name: operation for operation in operations}

    async def list_tools(
        _context: ServerRequestContext[Any], _params: types.PaginatedRequestParams | None
    ) -> types.ListToolsResult:
        return types.ListToolsResult(
            tools=[
                types.Tool(
                    name=operation.name,
                    description=operation.description,
                    input_schema=operation.input_schema,
                    annotations=_annotations(operation),
                )
                for operation in operations
            ]
        )

    async def call_tool(
        _context: ServerRequestContext[Any], params: types.CallToolRequestParams
    ) -> types.CallToolResult:
        operation = by_name.get(params.name)
        if operation is None:
            return _error(f"unknown tool: {params.name}")
        try:
            response = await call_operation(http, operation, params.arguments or {})
        except MissingArgumentError as error:
            return _error(str(error))
        except httpx2.TimeoutException:
            return _error(
                "the bridge did not answer in time; the outcome is not known. For an order, "
                "repeat the same call with the same id."
            )
        except httpx2.HTTPError as error:
            # Only the exception's type: its text is the transport's, not the bridge's.
            return _error(f"cannot reach the bridge at {config.bridge_url}: {type(error).__name__}")
        if not response.ok:
            # The bridge's problem document (RFC 9457) as it came: type, title, status,
            # detail, instance, code, retryable, mt5_retcode, mt5_last_error, errors.
            return types.CallToolResult(content=_text(response.problem()), is_error=True)
        return types.CallToolResult(content=_text("ok" if response.body is None else response.body))

    async def list_resources(
        _context: ServerRequestContext[Any], _params: types.PaginatedRequestParams | None
    ) -> types.ListResourcesResult:
        return types.ListResourcesResult(
            resources=[
                types.Resource(
                    uri=GUIDE_URI,
                    name="guide",
                    title="How to use the MT5 bridge",
                    description="Rules for reading market data and trading through these tools.",
                    mime_type="text/markdown",
                )
            ]
        )

    async def read_resource(
        _context: ServerRequestContext[Any], params: types.ReadResourceRequestParams
    ) -> types.ReadResourceResult:
        if params.uri != GUIDE_URI:
            raise ValueError(f"unknown resource: {params.uri}")
        guide = (
            resources.files("mt5_bridge.mcp_adapter")
            .joinpath("guide.md")
            .read_text(encoding="utf-8")
        )
        return types.ReadResourceResult(
            contents=[
                types.TextResourceContents(uri=GUIDE_URI, mime_type="text/markdown", text=guide)
            ]
        )

    def input_schema(name: str) -> dict[str, Any] | None:
        operation = by_name.get(name)
        return None if operation is None else operation.input_schema

    return Server(
        "mt5-bridge",
        version=contract_version,
        title="MT5 bridge",
        instructions=_INSTRUCTIONS,
        get_tool_input_schema=input_schema,
        on_list_tools=list_tools,
        on_call_tool=call_tool,
        on_list_resources=list_resources,
        on_read_resource=read_resource,
    )


def build_http_app(config: AdapterConfig, http: httpx2.AsyncClient, *, host: str) -> Starlette:
    """The adapter as a Streamable HTTP application, served at ``/mcp``.

    It keeps no session state, so every call stands alone.
    """
    server = build_server(config, http)
    return server.streamable_http_app(host=host, json_response=True, stateless_http=True)
