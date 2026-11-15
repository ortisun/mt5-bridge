"""A strictly-typed facade over FastAPI's ``TestClient``.

``starlette.testclient`` uses httpx2 when it is installed, and the MCP SDK in
the dev group installs it, so the responses here are ``httpx2.Response``.
Tests call this facade instead of ``TestClient``, and take ``Response`` from
here, so that one module names the HTTP library.
"""

from types import TracebackType
from typing import Any, Self

import httpx2
from fastapi import FastAPI
from fastapi.testclient import TestClient
from starlette.testclient import WebSocketTestSession

Response = httpx2.Response


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

    def get(self, path: str, **kwargs: Any) -> Response:
        return self._client.get(path, **kwargs)

    def post(self, path: str, **kwargs: Any) -> Response:
        return self._client.post(path, **kwargs)

    def patch(self, path: str, **kwargs: Any) -> Response:
        return self._client.patch(path, **kwargs)

    def delete(self, path: str, **kwargs: Any) -> Response:
        return self._client.delete(path, **kwargs)

    def websocket_connect(self, path: str, **kwargs: Any) -> WebSocketTestSession:
        return self._client.websocket_connect(path, **kwargs)
