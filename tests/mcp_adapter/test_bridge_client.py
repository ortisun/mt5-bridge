"""One call to the bridge: the request that leaves and the answer that is read."""

from typing import Any

import httpx2
import pytest
from opentelemetry.sdk.trace import TracerProvider

from mt5_bridge.mcp_adapter.bridge_client import BridgeResponse, call_operation
from mt5_bridge.mcp_adapter.contract_tools import Operation, load_operations

pytestmark = pytest.mark.asyncio

BRIDGE_URL = "http://bridge.test"


def operation(name: str) -> Operation:
    _version, operations = load_operations()
    return next(o for o in operations if o.name == name)


async def call(
    name: str, arguments: dict[str, Any], answer: httpx2.Response
) -> tuple[httpx2.Request, BridgeResponse]:
    """Calls one operation on a bridge that answers ``answer``; also returns what was sent."""
    sent: list[httpx2.Request] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        sent.append(request)
        return answer

    async with httpx2.AsyncClient(
        transport=httpx2.MockTransport(handler), base_url=BRIDGE_URL
    ) as http:
        response = await call_operation(http, operation(name), arguments)
    return sent[0], response


class TestRequest:
    async def test_arguments_go_to_the_path_the_query_and_the_body(self) -> None:
        request, _response = await call(
            "replace_order",
            {"client_order_id": "a/b", "operation_id": "op-1", "new_client_order_id": "c"},
            httpx2.Response(200, json={}),
        )

        assert request.method == "POST"
        assert request.url.raw_path == b"/v1/orders/a%2Fb/replace"
        assert b'"operation_id":"op-1"' in request.content
        assert b'client_order_id":"a/b' not in request.content

    async def test_the_active_span_travels_as_a_traceparent_header(self) -> None:
        tracer = TracerProvider().get_tracer("test")

        with tracer.start_as_current_span("agent turn") as span:
            request, _response = await call("get_account", {}, httpx2.Response(200, json={}))

        context = span.get_span_context()
        assert request.headers["traceparent"].startswith(
            f"00-{context.trace_id:032x}-{context.span_id:016x}-"
        )

    async def test_without_a_span_no_traceparent_is_sent(self) -> None:
        request, _response = await call("get_account", {}, httpx2.Response(200, json={}))

        assert "traceparent" not in request.headers


class TestProblem:
    async def test_a_problem_document_is_kept_member_for_member(self) -> None:
        document = {
            "type": "urn:mt5-bridge:problem:order-not-found",
            "title": "Unknown order",
            "status": 404,
            "detail": "no order has this client_order_id",
            "instance": "/v1/orders/x",
            "code": "OrderNotFound",
            "retryable": False,
            "mt5_retcode": None,
            "mt5_last_error": None,
            "a_member_added_later": "kept",
        }
        _request, response = await call(
            "get_order",
            {"client_order_id": "x"},
            httpx2.Response(
                404, json=document, headers={"content-type": "application/problem+json"}
            ),
        )

        assert response.ok is False
        assert response.problem() == document

    async def test_retry_after_is_added_to_the_problem_in_seconds(self) -> None:
        _request, response = await call(
            "get_account",
            {},
            httpx2.Response(
                503,
                json={"status": 503, "code": "TerminalUnavailable", "retryable": True},
                headers={"content-type": "application/problem+json", "retry-after": "5"},
            ),
        )

        assert response.problem()["retry_after_seconds"] == 5

    async def test_an_answer_that_is_not_a_problem_document_gets_the_same_members(self) -> None:
        _request, response = await call(
            "get_account", {}, httpx2.Response(502, text="<html>Bad Gateway</html>")
        )

        problem = response.problem()
        assert problem["type"] == "about:blank"
        assert problem["title"] == "Bad Gateway"
        assert problem["status"] == 502
        assert problem["code"] == "error"
        assert problem["retryable"] is False
        assert "html" not in problem["detail"]
