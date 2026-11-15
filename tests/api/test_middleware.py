"""MaxBodySizeMiddleware: rejects an oversized body before routing."""

from collections.abc import Iterable

import pytest

from mt5_bridge.api.middleware import MaxBodySizeMiddleware, Message, Receive, Scope, Send


async def _downstream_never_called(scope: Scope, receive: Receive, send: Send) -> None:
    del scope, receive, send
    raise AssertionError("the wrapped app must never run once the body is rejected")


async def _downstream_echoes_ok(scope: Scope, receive: Receive, send: Send) -> None:
    del scope
    while True:
        message = await receive()
        if not message.get("more_body", False):
            break
    await send({"type": "http.response.start", "status": 200, "headers": []})
    await send({"type": "http.response.body", "body": b"ok"})


def http_scope(*, headers: Iterable[tuple[bytes, bytes]] = ()) -> Scope:
    return {"type": "http", "method": "POST", "path": "/v1/orders", "headers": list(headers)}


def make_receive(chunks: list[bytes]) -> Receive:
    remaining = list(chunks)

    async def receive() -> Message:
        if not remaining:
            return {"type": "http.request", "body": b"", "more_body": False}
        body = remaining.pop(0)
        return {"type": "http.request", "body": body, "more_body": bool(remaining)}

    return receive


def make_send() -> tuple[Send, list[Message]]:
    sent: list[Message] = []

    async def send(message: Message) -> None:
        sent.append(message)

    return send, sent


@pytest.mark.asyncio
async def test_a_declared_content_length_over_the_limit_is_rejected_without_reading_the_body() -> (
    None
):
    middleware = MaxBodySizeMiddleware(_downstream_never_called, max_bytes=10)
    send, sent = make_send()

    await middleware(
        http_scope(headers=[(b"content-length", b"1000")]), make_receive([b"x" * 1000]), send
    )

    assert sent[0]["status"] == 413


@pytest.mark.asyncio
async def test_a_body_within_the_limit_reaches_the_wrapped_app() -> None:
    middleware = MaxBodySizeMiddleware(_downstream_echoes_ok, max_bytes=10)
    send, sent = make_send()

    await middleware(http_scope(headers=[(b"content-length", b"2")]), make_receive([b"ok"]), send)

    assert sent[0]["status"] == 200


@pytest.mark.asyncio
async def test_a_body_exceeding_the_limit_without_a_content_length_header_is_still_rejected() -> (
    None
):
    # No declared length to reject up front on, so the downstream app is a
    # realistic one that actually reads the body via `receive()` — that is
    # where the running-total check catches the oversized body mid-stream.
    middleware = MaxBodySizeMiddleware(_downstream_echoes_ok, max_bytes=5)
    send, sent = make_send()

    await middleware(http_scope(), make_receive([b"12345", b"6"]), send)

    assert sent[0]["status"] == 413


@pytest.mark.asyncio
async def test_a_non_http_scope_passes_through_untouched() -> None:
    calls: list[str] = []

    async def downstream(scope: Scope, receive: Receive, send: Send) -> None:
        del scope, receive, send
        calls.append("called")

    middleware = MaxBodySizeMiddleware(downstream, max_bytes=5)
    send, _sent = make_send()

    await middleware({"type": "lifespan"}, make_receive([]), send)

    assert calls == ["called"]
