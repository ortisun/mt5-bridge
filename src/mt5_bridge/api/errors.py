"""Problem Details for HTTP APIs (RFC 9457): the one error format of the bridge.

Every 4xx/5xx body is an ``application/problem+json`` document with the
standard members ``type``, ``title``, ``status``, ``detail`` and ``instance``
and the bridge's own extension members ``code``, ``retryable``,
``mt5_retcode`` and ``mt5_last_error`` (plan §114); a validation problem also
carries ``errors``. The catalogue below is the single source of every problem
type; ``docs/errors.md`` lists the same types for clients.

The gRPC API reports the same problems: each type names the gRPC status a
failed call ends with (``grpc_status``), and ``api.grpc_api.calls`` sends the
same members in a ``Problem`` message.
"""

import json
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from http import HTTPStatus
from typing import Any

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from mt5_bridge.core.order_service import (
    ClockDriftExceeded,
    IdempotencyConflict,
    NotReconcilable,
    OrderNotAmendable,
    OrderNotFound,
    WorkerUnavailable,
)
from mt5_bridge.core.worker import WorkerQueueSaturated, WorkerStopped
from mt5_bridge.mt5.errors import Mt5ConnectionLost

PROBLEM_MEDIA_TYPE = "application/problem+json"
# A URN, not a URL: the type is an identifier a client compares as a string,
# and the bridge has no public host name to serve documentation from.
PROBLEM_TYPE_PREFIX = "urn:mt5-bridge:problem:"
# RFC 9457 §4.2.1: the type of a problem that says no more than its status code.
ABOUT_BLANK = "about:blank"


@dataclass(frozen=True, slots=True)
class ProblemType:
    """One kind of error: its type URI, title, HTTP status, ``code`` and the
    gRPC status code (by name, as in ``grpc.StatusCode``) of the same error."""

    slug: str
    title: str
    status: int
    code: str
    grpc_status: str
    retryable: bool = False
    # RFC 9110 §10.2.3: sent as ``Retry-After`` (seconds) when set.
    retry_after_seconds: int | None = None

    @property
    def uri(self) -> str:
        return f"{PROBLEM_TYPE_PREFIX}{self.slug}"


SYMBOL_NOT_FOUND = ProblemType("symbol-not-found", "Unknown symbol", 404, "NotFound", "NOT_FOUND")
ORDER_NOT_FOUND = ProblemType("order-not-found", "Unknown order", 404, "OrderNotFound", "NOT_FOUND")
IDEMPOTENCY_CONFLICT = ProblemType(
    "idempotency-conflict", "Idempotency key reused", 409, "IdempotencyConflict", "ALREADY_EXISTS"
)
ORDER_STATE_CONFLICT = ProblemType(
    "order-state-conflict",
    "Order state does not allow this",
    409,
    "OrderStateConflict",
    "FAILED_PRECONDITION",
)
UNSUPPORTED_VENUE_CAPABILITY = ProblemType(
    "unsupported-venue-capability",
    "Unsupported venue capability",
    409,
    "UnsupportedVenueCapability",
    "FAILED_PRECONDITION",
)
PAYLOAD_TOO_LARGE = ProblemType(
    "payload-too-large", "Request body too large", 413, "PayloadTooLarge", "RESOURCE_EXHAUSTED"
)
VALIDATION_ERROR = ProblemType(
    "validation-error", "Invalid request", 422, "ValidationError", "INVALID_ARGUMENT"
)
INTERNAL_ERROR = ProblemType("internal-error", "Internal error", 500, "InternalError", "INTERNAL")
WORKER_UNAVAILABLE = ProblemType(
    "worker-unavailable",
    "Bridge worker unavailable",
    503,
    "WorkerUnavailable",
    "UNAVAILABLE",
    retryable=True,
    retry_after_seconds=1,
)
TERMINAL_UNAVAILABLE = ProblemType(
    "terminal-unavailable",
    "Terminal unavailable",
    503,
    "TerminalUnavailable",
    "UNAVAILABLE",
    retryable=True,
    retry_after_seconds=5,
)
CLOCK_DRIFT_EXCEEDED = ProblemType(
    "clock-drift-exceeded",
    "Clock drift exceeds the threshold",
    503,
    "ClockDriftExceeded",
    "UNAVAILABLE",
    retryable=True,
    retry_after_seconds=60,
)

CATALOGUE: tuple[ProblemType, ...] = (
    SYMBOL_NOT_FOUND,
    ORDER_NOT_FOUND,
    IDEMPOTENCY_CONFLICT,
    ORDER_STATE_CONFLICT,
    UNSUPPORTED_VENUE_CAPABILITY,
    PAYLOAD_TOO_LARGE,
    VALIDATION_ERROR,
    INTERNAL_ERROR,
    WORKER_UNAVAILABLE,
    TERMINAL_UNAVAILABLE,
    CLOCK_DRIFT_EXCEEDED,
)

# Exceptions the service raises, and the problem each one is reported as.
# Starlette picks the handler by walking the exception's MRO, so no entry
# here catches a wider class than the one named.
_EXCEPTION_PROBLEMS: tuple[tuple[type[Exception], ProblemType], ...] = (
    (IdempotencyConflict, IDEMPOTENCY_CONFLICT),
    (OrderNotFound, ORDER_NOT_FOUND),
    (NotReconcilable, ORDER_STATE_CONFLICT),
    (OrderNotAmendable, ORDER_STATE_CONFLICT),
    (WorkerUnavailable, WORKER_UNAVAILABLE),
    (WorkerQueueSaturated, WORKER_UNAVAILABLE),
    (WorkerStopped, WORKER_UNAVAILABLE),
    (Mt5ConnectionLost, TERMINAL_UNAVAILABLE),
    (ClockDriftExceeded, CLOCK_DRIFT_EXCEEDED),
)

INTERNAL_ERROR_DETAIL = "the bridge could not process the request; the cause is in its log"


class ProblemError(Exception):
    """Raised by a route or dependency to answer with one catalogue problem."""

    def __init__(
        self,
        problem_type: ProblemType,
        detail: str,
        *,
        errors: Sequence[Mapping[str, str]] | None = None,
    ) -> None:
        super().__init__(detail)
        self.problem_type = problem_type
        self.detail = detail
        self.errors = errors


def problem_for(error: Exception) -> ProblemType | None:
    """The catalogue problem a service exception is reported as, or ``None``
    for an exception the catalogue does not name (an internal error)."""
    for exception_class, problem_type in _EXCEPTION_PROBLEMS:
        if isinstance(error, exception_class):
            return problem_type
    return None


def problem_body(
    problem_type: ProblemType,
    *,
    detail: str,
    instance: str | None = None,
    errors: Sequence[Mapping[str, str]] | None = None,
    mt5_retcode: int | None = None,
    mt5_last_error: str | None = None,
) -> dict[str, object]:
    return _body(
        type_uri=problem_type.uri,
        title=problem_type.title,
        status=problem_type.status,
        code=problem_type.code,
        retryable=problem_type.retryable,
        detail=detail,
        instance=instance,
        errors=errors,
        mt5_retcode=mt5_retcode,
        mt5_last_error=mt5_last_error,
    )


def problem_headers(
    problem_type: ProblemType, extra: Mapping[str, str] | None = None
) -> dict[str, str]:
    """The headers a problem of this type carries, without ``Content-Type``."""
    headers = dict(extra or {})
    if problem_type.retry_after_seconds is not None:
        headers["Retry-After"] = str(problem_type.retry_after_seconds)
    return headers


def problem_response(
    problem_type: ProblemType,
    *,
    detail: str,
    instance: str | None = None,
    errors: Sequence[Mapping[str, str]] | None = None,
    headers: Mapping[str, str] | None = None,
) -> JSONResponse:
    return JSONResponse(
        status_code=problem_type.status,
        content=problem_body(problem_type, detail=detail, instance=instance, errors=errors),
        headers=problem_headers(problem_type, headers),
        media_type=PROBLEM_MEDIA_TYPE,
    )


def encode_problem(body: Mapping[str, object]) -> bytes:
    """For the plain ASGI middleware, which cannot build a ``JSONResponse``."""
    return json.dumps(body, separators=(",", ":")).encode()


def validation_errors(errors: Sequence[Mapping[str, Any]]) -> list[dict[str, str]]:
    """Pydantic's error list as the ``errors`` member: where each fault is and what it is.

    A body fault is located by a JSON Pointer (RFC 6901) in URI-fragment
    form, as in RFC 9457 §3; a query, path or header fault by ``in`` and
    ``parameter``. Pydantic's copy of the rejected input is left out: it may
    hold a secret.
    """
    items: list[dict[str, str]] = []
    for error in errors:
        location: Sequence[object] = error.get("loc", ())
        parts = [str(part) for part in location]
        source, path = (parts[0], parts[1:]) if parts else ("body", [])
        detail = str(error.get("msg", "invalid value"))
        if source != "body":
            items.append({"in": source, "parameter": ".".join(path), "detail": detail})
        elif error.get("type") == "json_invalid":
            # Pydantic locates malformed JSON by character offset, not by member.
            items.append({"pointer": "#", "detail": detail})
        else:
            items.append(
                {"pointer": "#" + "".join(f"/{_escape(p)}" for p in path), "detail": detail}
            )
    return items


def install_error_handlers(app: FastAPI) -> None:
    """Register every handler: after this no error leaves the app in another format."""
    for exception_class, problem_type in _EXCEPTION_PROBLEMS:
        app.add_exception_handler(exception_class, _catalogue_handler(problem_type))
    app.add_exception_handler(ProblemError, _handle_problem_error)
    app.add_exception_handler(StarletteHTTPException, _handle_http_exception)
    app.add_exception_handler(RequestValidationError, _handle_validation_error)
    app.add_exception_handler(Exception, _handle_unexpected_error)


def _body(
    *,
    type_uri: str,
    title: str,
    status: int,
    code: str,
    retryable: bool,
    detail: str,
    instance: str | None,
    errors: Sequence[Mapping[str, str]] | None = None,
    mt5_retcode: int | None = None,
    mt5_last_error: str | None = None,
) -> dict[str, object]:
    body: dict[str, object] = {
        "type": type_uri,
        "title": title,
        "status": status,
        "detail": detail,
    }
    if instance is not None:
        body["instance"] = instance
    body["code"] = code
    body["retryable"] = retryable
    body["mt5_retcode"] = mt5_retcode
    body["mt5_last_error"] = mt5_last_error
    if errors is not None:
        body["errors"] = [dict(error) for error in errors]
    return body


def _escape(segment: str) -> str:
    return segment.replace("~", "~0").replace("/", "~1")


def _catalogue_handler(
    problem_type: ProblemType,
) -> Callable[[Request, Exception], Awaitable[JSONResponse]]:
    async def handler(request: Request, exc: Exception) -> JSONResponse:
        return problem_response(problem_type, detail=str(exc), instance=request.url.path)

    return handler


async def _handle_problem_error(request: Request, exc: Exception) -> JSONResponse:
    if not isinstance(exc, ProblemError):  # pragma: no cover - registered for ProblemError
        raise exc
    return problem_response(
        exc.problem_type,
        detail=exc.detail,
        instance=request.url.path,
        errors=exc.errors,
    )


async def _handle_http_exception(request: Request, exc: Exception) -> JSONResponse:
    """A status the framework raised itself (unknown path, wrong method): it
    says no more than its status code, so its type is ``about:blank``. The
    exception's headers are kept: a 405 must carry ``Allow`` (RFC 9110 §15.5.6)."""
    if not isinstance(exc, StarletteHTTPException):  # pragma: no cover
        raise exc
    title = HTTPStatus(exc.status_code).phrase
    return JSONResponse(
        status_code=exc.status_code,
        content=_body(
            type_uri=ABOUT_BLANK,
            title=title,
            status=exc.status_code,
            code="error",
            retryable=False,
            detail=str(exc.detail),
            instance=request.url.path,
        ),
        headers=exc.headers,
        media_type=PROBLEM_MEDIA_TYPE,
    )


async def _handle_validation_error(request: Request, exc: Exception) -> JSONResponse:
    if not isinstance(exc, RequestValidationError):  # pragma: no cover
        raise exc
    errors = validation_errors(exc.errors())
    return problem_response(
        VALIDATION_ERROR,
        detail=f"the request has {len(errors)} invalid value(s); see errors",
        instance=request.url.path,
        errors=errors,
    )


async def _handle_unexpected_error(request: Request, exc: Exception) -> JSONResponse:
    """Any exception no other handler claimed. The body never quotes the
    exception: its text may hold a secret or an internal path. Starlette
    re-raises it after this response, so the server still logs the traceback."""
    del exc
    return problem_response(INTERNAL_ERROR, detail=INTERNAL_ERROR_DETAIL, instance=request.url.path)
