"""Calls one contract operation on the bridge over HTTP."""

from collections.abc import Mapping
from dataclasses import dataclass
from http import HTTPStatus
from typing import Any, cast
from urllib.parse import quote

import httpx2
from opentelemetry import propagate

from mt5_bridge.mcp_adapter.contract_tools import Operation

PROBLEM_MEDIA_TYPE = "application/problem+json"
# RFC 9457 §4.2.1: the type of a problem that says no more than its status code.
ABOUT_BLANK = "about:blank"


@dataclass(frozen=True, slots=True)
class BridgeResponse:
    status: int
    body: Any
    """The decoded JSON body, the text of a body that is not JSON, or ``None``
    when the bridge sent none."""
    media_type: str
    retry_after_seconds: int | None
    """The ``Retry-After`` header of a 503, in seconds."""

    @property
    def ok(self) -> bool:
        return self.status < 400

    def problem(self) -> dict[str, Any]:
        """The Problem Details document (RFC 9457) of a failed call.

        The bridge's own document is returned member for member (docs/errors.md),
        with ``retry_after_seconds`` added when the response said when to retry.
        An answer that is not a problem document, which only something between
        the adapter and the bridge can produce, becomes an ``about:blank``
        problem so that the agent always reads the same members.
        """
        body: Any = self.body
        document: dict[str, Any]
        if self.media_type == PROBLEM_MEDIA_TYPE and isinstance(body, dict):
            document = dict(cast("dict[str, Any]", body))
        else:
            document = {
                "type": ABOUT_BLANK,
                "title": _status_phrase(self.status),
                "status": self.status,
                "detail": "the answer was not a problem document; it may not come from the bridge",
                "code": "error",
                "retryable": False,
            }
        if self.retry_after_seconds is not None:
            document["retry_after_seconds"] = self.retry_after_seconds
        return document


class MissingArgumentError(ValueError):
    pass


def _status_phrase(status: int) -> str:
    try:
        return HTTPStatus(status).phrase
    except ValueError:
        return "Error"


def _retry_after_seconds(response: httpx2.Response) -> int | None:
    value = response.headers.get("retry-after", "")
    return int(value) if value.isdecimal() else None


def _trace_headers() -> dict[str, str]:
    """W3C trace context (``traceparent``) of the active span; empty without one.

    The bridge continues the trace from this header (docs/observability.md).
    """
    headers: dict[str, str] = {}
    propagate.inject(headers)
    return headers


async def call_operation(
    http: httpx2.AsyncClient,
    operation: Operation,
    arguments: Mapping[str, Any],
) -> BridgeResponse:
    path = operation.path
    for name in operation.path_params:
        value = arguments.get(name)
        if value is None or value == "":
            raise MissingArgumentError(f"missing required argument: {name}")
        path = path.replace("{" + name + "}", quote(str(value), safe=""))

    query = {
        name: arguments[name] for name in operation.query_params if arguments.get(name) is not None
    }
    body = {
        name: arguments[name] for name in operation.body_params if arguments.get(name) is not None
    }

    response = await http.request(
        operation.method,
        path,
        params=query or None,
        json=body if operation.body_params else None,
        headers=_trace_headers(),
    )
    decoded: Any = None
    if response.content:
        try:
            decoded = response.json()
        except ValueError:
            decoded = response.text
    return BridgeResponse(
        status=response.status_code,
        body=decoded,
        media_type=response.headers.get("content-type", "").partition(";")[0].strip().lower(),
        retry_after_seconds=_retry_after_seconds(response),
    )
