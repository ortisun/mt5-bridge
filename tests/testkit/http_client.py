"""A strictly-typed facade over FastAPI's ``TestClient``.

This installation warns that httpx support in ``starlette.testclient`` is
being replaced ("install httpx2 instead"); during that transition,
``TestClient.get/post/patch/delete`` resolve to an unknown type under
strict Pyright, even though they work correctly at runtime (every test using
this facade passes). Containing the four resulting ``pyright: ignore``
markers to these five methods — instead of an unresolved type at every one
of dozens of call sites — is the documented, narrow exception (the root
cause is a third-party library's in-progress migration, not a defect this
project can fix or that these markers hide).
"""

from types import TracebackType
from typing import Any, Self

import httpx
from fastapi import FastAPI
from fastapi.testclient import TestClient
from starlette.testclient import WebSocketTestSession


class TypedTestClient:
    def __init__(self, app: FastAPI, *, raise_server_exceptions: bool = True) -> None:
        """``raise_server_exceptions=False`` returns the 500 response a real
        client would get instead of re-raising the server's exception."""
        self._client = TestClient(app, raise_server_exceptions=raise_server_exceptions)

    def __enter__(self) -> Self:
        self._client.__enter__()
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self._client.__exit__(exc_type, exc_value, traceback)

    def get(self, path: str, **kwargs: Any) -> httpx.Response:
        return self._client.get(path, **kwargs)  # pyright: ignore[reportUnknownMemberType, reportUnknownVariableType]

    def post(self, path: str, **kwargs: Any) -> httpx.Response:
        return self._client.post(path, **kwargs)  # pyright: ignore[reportUnknownMemberType, reportUnknownVariableType]

    def patch(self, path: str, **kwargs: Any) -> httpx.Response:
        return self._client.patch(path, **kwargs)  # pyright: ignore[reportUnknownMemberType, reportUnknownVariableType]

    def delete(self, path: str, **kwargs: Any) -> httpx.Response:
        return self._client.delete(path, **kwargs)  # pyright: ignore[reportUnknownMemberType, reportUnknownVariableType]

    def websocket_connect(self, path: str, **kwargs: Any) -> WebSocketTestSession:
        return self._client.websocket_connect(path, **kwargs)
