"""The MCP adapter end to end: an MCP client, the adapter, and the real bridge
application over the fake terminal. Nothing leaves the process."""

import json
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from typing import Any

import httpx2
import pytest
from fastapi import FastAPI
from mcp import Client, types
from mcp.client.streamable_http import streamable_http_client

from mt5_bridge.mcp_adapter.config import AdapterConfig
from mt5_bridge.mcp_adapter.contract_tools import load_operations
from mt5_bridge.mcp_adapter.server import GUIDE_URI, build_http_app, build_server
from mt5_bridge.mt5.fake_client import FakeMt5Client

from testkit.app_builder import build_test_app
from testkit.fixtures import PETR4

pytestmark = pytest.mark.asyncio

BRIDGE_URL = "http://bridge.test"
CONFIG = AdapterConfig(bridge_url=BRIDGE_URL, ca_file=None, timeout_seconds=5)


def bridge_http(app: FastAPI) -> httpx2.AsyncClient:
    return httpx2.AsyncClient(transport=httpx2.ASGITransport(app=app), base_url=BRIDGE_URL)


@asynccontextmanager
async def agent_and_terminal() -> AsyncGenerator[tuple[Client, FakeMt5Client]]:
    """An MCP client connected in-process to an adapter in front of a fresh bridge."""
    app, terminal, _state = build_test_app()
    async with bridge_http(app) as http:
        async with Client(build_server(CONFIG, http)) as client:
            yield client, terminal


@asynccontextmanager
async def agent() -> AsyncGenerator[Client]:
    async with agent_and_terminal() as (client, _terminal):
        yield client


def text(result: types.CallToolResult) -> str:
    block = result.content[0]
    assert isinstance(block, types.TextContent)
    return block.text


def payload(result: types.CallToolResult) -> Any:
    return json.loads(text(result))


def order(client_order_id: str = "agent-1") -> dict[str, Any]:
    return {
        "client_order_id": client_order_id,
        "symbol": PETR4,
        "side": "BUY",
        "type": "LIMIT",
        "volume": "100",
        "time_in_force": "DAY",
        "price": "30.00",
    }


class TestOfferedTools:
    async def test_every_contract_operation_is_offered(self) -> None:
        _version, operations = load_operations()

        async with agent() as client:
            tools = (await client.list_tools()).tools

        assert {tool.name for tool in tools} == {operation.name for operation in operations}

    async def test_the_hints_tell_reading_from_trading(self) -> None:
        async with agent() as client:
            tools = {tool.name: tool.annotations for tool in (await client.list_tools()).tools}

        account, submit, check = tools["get_account"], tools["submit_order"], tools["check_order"]
        assert account is not None and submit is not None and check is not None
        assert account.read_only_hint is True
        assert submit.read_only_hint is False
        assert submit.destructive_hint is True
        assert submit.idempotent_hint is True
        assert check.destructive_hint is False

    async def test_the_server_gives_instructions_and_a_guide(self) -> None:
        async with agent() as client:
            instructions = client.instructions
            guide = await client.read_resource(GUIDE_URI)

        assert instructions is not None
        assert GUIDE_URI in instructions
        content = guide.contents[0]
        assert isinstance(content, types.TextResourceContents)
        assert "client_order_id" in content.text
        assert "retryable" in content.text


class TestReading:
    async def test_a_tool_returns_the_bridge_response_with_decimals_as_strings(self) -> None:
        async with agent() as client:
            result = await client.call_tool("get_account", {})

        assert result.is_error is False
        assert payload(result)["balance"] == "100000.00"

    async def test_path_and_query_arguments_reach_the_bridge(self) -> None:
        async with agent() as client:
            symbol = await client.call_tool("get_symbol", {"symbol": PETR4})
            bars = await client.call_tool(
                "get_bars", {"symbol": PETR4, "timeframe": "M1", "count": 5}
            )

        assert payload(symbol)["name"] == PETR4
        assert bars.is_error is False
        assert isinstance(payload(bars), list)


class TestErrors:
    async def test_a_bridge_error_is_the_problem_document_of_the_bridge(self) -> None:
        async with agent() as client:
            result = await client.call_tool("get_symbol", {"symbol": "NOPE"})

        assert result.is_error is True
        problem = payload(result)
        assert problem["type"] == "urn:mt5-bridge:problem:symbol-not-found"
        assert problem["title"] == "Unknown symbol"
        assert problem["status"] == 404
        assert problem["instance"] == "/v1/symbols/NOPE"
        assert problem["code"] == "NotFound"
        assert problem["retryable"] is False
        assert "NOPE" in problem["detail"]
        assert {"mt5_retcode", "mt5_last_error"} <= set(problem)
        assert "retry_after_seconds" not in problem

    async def test_an_invalid_argument_is_located_by_the_errors_member(self) -> None:
        async with agent() as client:
            result = await client.call_tool("get_bars", {"symbol": PETR4, "timeframe": "Z9"})

        assert result.is_error is True
        problem = payload(result)
        assert problem["status"] == 422
        assert problem["code"] == "ValidationError"
        assert problem["retryable"] is False
        assert [(e["in"], e["parameter"]) for e in problem["errors"]] == [("query", "timeframe")]

    async def test_a_disconnected_terminal_is_retryable_and_says_when(self) -> None:
        async with agent_and_terminal() as (client, terminal):
            terminal.simulate_disconnect()
            result = await client.call_tool("get_account", {})

        assert result.is_error is True
        problem = payload(result)
        assert problem["status"] == 503
        assert problem["code"] == "TerminalUnavailable"
        assert problem["retryable"] is True
        assert problem["retry_after_seconds"] == 5

    async def test_an_unreachable_bridge_is_an_error_result_not_a_crash(self) -> None:
        def refuse(request: httpx2.Request) -> httpx2.Response:
            raise httpx2.ConnectError("refused", request=request)

        async with httpx2.AsyncClient(
            transport=httpx2.MockTransport(refuse), base_url=BRIDGE_URL
        ) as http:
            async with Client(build_server(CONFIG, http)) as client:
                result = await client.call_tool("get_account", {})

        assert result.is_error is True
        assert f"cannot reach the bridge at {BRIDGE_URL}" in text(result)

    async def test_a_bridge_that_does_not_answer_in_time_leaves_the_outcome_unknown(self) -> None:
        def stall(request: httpx2.Request) -> httpx2.Response:
            raise httpx2.ReadTimeout("timed out", request=request)

        async with httpx2.AsyncClient(
            transport=httpx2.MockTransport(stall), base_url=BRIDGE_URL
        ) as http:
            async with Client(build_server(CONFIG, http)) as client:
                result = await client.call_tool("submit_order", order())

        assert result.is_error is True
        assert "the outcome is not known" in text(result)


class TestTrading:
    async def test_an_order_is_submitted_once_per_client_order_id(self) -> None:
        async with agent() as client:
            first = await client.call_tool("submit_order", order())
            replay = await client.call_tool("submit_order", order())
            open_orders = await client.call_tool("list_open_orders", {})

        assert first.is_error is False
        assert payload(first)["client_order_id"] == "agent-1"
        assert payload(replay) == payload(first)
        assert len(payload(open_orders)) == 1

    async def test_a_reused_client_order_id_with_another_payload_is_a_conflict(self) -> None:
        async with agent() as client:
            await client.call_tool("submit_order", order())
            result = await client.call_tool("submit_order", {**order(), "volume": "200"})

        assert result.is_error is True
        assert payload(result)["status"] == 409
        assert payload(result)["code"] == "IdempotencyConflict"

    async def test_a_body_only_tool_and_a_path_tool_work_together(self) -> None:
        async with agent() as client:
            await client.call_tool("submit_order", order())
            canceled = await client.call_tool("cancel_order", {"client_order_id": "agent-1"})
            halted = await client.call_tool("activate_kill_switch", {"action": "HALT_NEW_ORDERS"})
            rearmed = await client.call_tool("rearm_kill_switch", {})
            heartbeat = await client.call_tool("heartbeat", {})

        assert payload(canceled)["state"] == "CANCELED"
        assert payload(halted)["state"] == "HALTED"
        assert payload(rearmed)["state"] == "NORMAL"
        assert heartbeat.is_error is False
        assert text(heartbeat) == "ok"


class TestHttpTransport:
    async def test_a_caller_over_streamable_http_reaches_the_bridge(self) -> None:
        app, _terminal, _state = build_test_app()
        async with bridge_http(app) as http:
            mcp_app = build_http_app(CONFIG, http, host="127.0.0.1")
            async with mcp_app.router.lifespan_context(mcp_app):
                caller = httpx2.AsyncClient(transport=httpx2.ASGITransport(app=mcp_app))
                transport = streamable_http_client("http://127.0.0.1:8766/mcp", http_client=caller)
                async with Client(transport) as client:
                    ok = await client.call_tool("get_account", {})
                    refused = await client.call_tool("get_symbol", {"symbol": "NOPE"})

        assert ok.is_error is False
        assert payload(ok)["balance"] == "100000.00"
        assert refused.is_error is True
        assert payload(refused)["code"] == "NotFound"
