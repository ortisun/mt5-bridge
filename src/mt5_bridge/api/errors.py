"""The single error envelope (plan §114): code, message, mt5_retcode,
mt5_last_error, retryable. Every 4xx/5xx body uses this shape, never
FastAPI's default ``{"detail": ...}`` wrapper.
"""

from fastapi import HTTPException, Request
from fastapi.responses import JSONResponse

from mt5_bridge.core.order_service import IdempotencyConflict, OrderNotFound, WorkerUnavailable


def error_body(
    *,
    code: str,
    message: str,
    mt5_retcode: int | None = None,
    mt5_last_error: str | None = None,
    retryable: bool = False,
) -> dict[str, object]:
    return {
        "code": code,
        "message": message,
        "mt5_retcode": mt5_retcode,
        "mt5_last_error": mt5_last_error,
        "retryable": retryable,
    }


def http_error(
    status_code: int,
    *,
    code: str,
    message: str,
    mt5_retcode: int | None = None,
    mt5_last_error: str | None = None,
    retryable: bool = False,
) -> HTTPException:
    return HTTPException(
        status_code=status_code,
        detail=error_body(
            code=code,
            message=message,
            mt5_retcode=mt5_retcode,
            mt5_last_error=mt5_last_error,
            retryable=retryable,
        ),
    )


async def handle_http_exception(request: Request, exc: Exception) -> JSONResponse:
    del request
    if not isinstance(
        exc, HTTPException
    ):  # pragma: no cover - handler is registered for HTTPException
        raise exc
    body = (
        exc.detail
        if isinstance(exc.detail, dict)
        else error_body(code="error", message=str(exc.detail))
    )
    return JSONResponse(status_code=exc.status_code, content=body)


async def handle_idempotency_conflict(request: Request, exc: Exception) -> JSONResponse:
    del request
    if not isinstance(exc, IdempotencyConflict):  # pragma: no cover
        raise exc
    return JSONResponse(
        status_code=409,
        content=error_body(
            code="IdempotencyConflict",
            message=str(exc),
            retryable=False,
        ),
    )


async def handle_order_not_found(request: Request, exc: Exception) -> JSONResponse:
    del request
    if not isinstance(exc, OrderNotFound):  # pragma: no cover
        raise exc
    return JSONResponse(
        status_code=404,
        content=error_body(code="OrderNotFound", message=str(exc), retryable=False),
    )


async def handle_worker_unavailable(request: Request, exc: Exception) -> JSONResponse:
    del request
    if not isinstance(exc, WorkerUnavailable):  # pragma: no cover
        raise exc
    return JSONResponse(
        status_code=503,
        content=error_body(code="WorkerUnavailable", message=str(exc), retryable=True),
    )


async def handle_value_error(request: Request, exc: Exception) -> JSONResponse:
    del request
    if not isinstance(exc, ValueError):  # pragma: no cover
        raise exc
    return JSONResponse(
        status_code=409,
        content=error_body(code="ValidationError", message=str(exc), retryable=False),
    )
