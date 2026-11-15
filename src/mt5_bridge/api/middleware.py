"""ASGI middleware that runs ahead of routing (plan §112).

Deliberately plain ASGI rather than Starlette's ``BaseHTTPMiddleware``: the
oversized-body case must be rejected before a route, a dependency, or
Pydantic validation ever sees the body, and a request this large should
never be buffered in full just to measure it.
"""

import json
from collections.abc import Awaitable, Callable, Iterable
from typing import Any

from mt5_bridge.api.errors import error_body

Scope = dict[str, Any]
Message = dict[str, Any]
Receive = Callable[[], Awaitable[Message]]
Send = Callable[[Message], Awaitable[None]]
ASGIApp = Callable[[Scope, Receive, Send], Awaitable[None]]


class BodyTooLarge(Exception):
    pass


class MaxBodySizeMiddleware:
    """Rejects a request body over ``max_bytes`` with ``413`` before it
    reaches routing. Checked twice: the declared ``Content-Length`` up
    front (the common case — rejected without reading a single byte of
    body), and a running total of bytes actually received, which also
    catches a missing or understated length.
    """

    def __init__(self, app: ASGIApp, *, max_bytes: int) -> None:
        self._app = app
        self._max_bytes = max_bytes

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self._app(scope, receive, send)
            return

        declared = _declared_content_length(scope.get("headers", ()))
        if declared is not None and declared > self._max_bytes:
            await _send_413(send)
            return

        received = 0

        async def guarded_receive() -> Message:
            nonlocal received
            message = await receive()
            if message["type"] == "http.request":
                received += len(message.get("body") or b"")
                if received > self._max_bytes:
                    raise BodyTooLarge()
            return message

        try:
            await self._app(scope, guarded_receive, send)
        except BodyTooLarge:
            await _send_413(send)


def _declared_content_length(headers: Iterable[tuple[bytes, bytes]]) -> int | None:
    for raw_name, raw_value in headers:
        if raw_name.lower() == b"content-length":
            try:
                return int(raw_value)
            except ValueError:
                return None
    return None


async def _send_413(send: Send) -> None:
    body = json.dumps(
        error_body(
            code="PayloadTooLarge",
            message="request body exceeds the configured size limit",
            retryable=False,
        )
    ).encode()
    await send(
        {
            "type": "http.response.start",
            "status": 413,
            "headers": [(b"content-type", b"application/json")],
        }
    )
    await send({"type": "http.response.body", "body": body})
