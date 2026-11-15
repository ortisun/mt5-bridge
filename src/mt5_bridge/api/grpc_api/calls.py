"""What every gRPC call shares: the trading gate, the error mapping, the
audit record and the caller's address.

The rules are the REST ones. A handler raises the same ``ProblemError`` a
route raises, and the service exceptions map to the same problem types
(``api.errors``); the type's ``grpc_status`` is the status the call ends
with, and the members of the REST problem document travel as a ``Problem``
message in the trailing metadata. A write call is audited like a REST write
(``api.audit``).
"""

import functools
import hashlib
import logging
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, ClassVar, Never, cast
from urllib.parse import unquote

import grpc
from google.protobuf.message import Message

from mt5_bridge.api.app_state import AppState
from mt5_bridge.api.audit import GRPC_METHOD
from mt5_bridge.api.errors import (
    INTERNAL_ERROR,
    INTERNAL_ERROR_DETAIL,
    VALIDATION_ERROR,
    ProblemError,
    ProblemType,
    problem_for,
)
from mt5_bridge.api.grpc_api.generated import mt5_bridge_pb2 as pb
from mt5_bridge.api.grpc_api.telemetry import Context, GrpcTelemetry

PROBLEM_METADATA_KEY = "mt5-bridge-problem-bin"

_LOG = logging.getLogger("mt5_bridge.grpc")
_IP_PEER_SCHEMES = frozenset({"ipv4", "ipv6"})


@dataclass(frozen=True, slots=True)
class Failure:
    """Why a call fails: one catalogue problem and what went wrong this time."""

    problem_type: ProblemType
    detail: str
    errors: Sequence[Mapping[str, str]] | None = None

    @property
    def status(self) -> grpc.StatusCode:
        return grpc.StatusCode[self.problem_type.grpc_status]

    @property
    def unexpected(self) -> bool:
        return self.problem_type is INTERNAL_ERROR


def invalid_value(field: str, detail: str) -> ProblemError:
    """A validation problem that names the request field at fault."""
    message = f"{field}: {detail}"
    return ProblemError(VALIDATION_ERROR, message, errors=[{"field": field, "detail": detail}])


def failure_for(error: Exception) -> Failure:
    """The failure an exception is reported as. One the catalogue does not
    name is an internal error, and its text is never quoted: it may hold a
    secret or an internal path."""
    if isinstance(error, ProblemError):
        return Failure(error.problem_type, error.detail, error.errors)
    problem_type = problem_for(error)
    if problem_type is None:
        return Failure(INTERNAL_ERROR, INTERNAL_ERROR_DETAIL)
    return Failure(problem_type, str(error))


def problem_message(failure: Failure, *, instance: str) -> pb.Problem:
    """The ``Problem`` of a failure: the members ``api.errors.problem_body``
    writes in the REST document, without the HTTP status."""
    problem_type = failure.problem_type
    return pb.Problem(
        type=problem_type.uri,
        title=problem_type.title,
        detail=failure.detail,
        instance=instance,
        code=problem_type.code,
        retryable=problem_type.retryable,
        retry_after_seconds=problem_type.retry_after_seconds,
        errors=[
            pb.InvalidValue(field=error.get("field", ""), detail=error.get("detail", ""))
            for error in failure.errors or ()
        ],
    )


async def abort(context: Context, failure: Failure, *, method: str) -> Never:
    problem = problem_message(failure, instance=f"/{method}")
    await context.abort(
        failure.status,
        failure.detail,
        trailing_metadata=((PROBLEM_METADATA_KEY, problem.SerializeToString()),),
    )


def report(failure: Failure, error: Exception, *, method: str) -> None:
    """Log an error the catalogue does not name; the caller is told nothing of it."""
    if failure.unexpected:
        _LOG.error("unhandled error in %s", method, exc_info=error)


class Service:
    """What the three servicers share: the application state, the telemetry
    of the server, and the full name of their service."""

    service_name: ClassVar[str]

    def __init__(self, state: AppState, telemetry: GrpcTelemetry) -> None:
        self.state = state
        self.telemetry = telemetry


def rpc[F: Callable[..., Awaitable[Any]]](handler: F) -> F:
    """Wrap a unary handler: the call runs inside its server span, and an
    exception ends it with the status and the ``Problem`` of its problem type."""
    name = handler.__name__

    @functools.wraps(handler)
    async def wrapper(self: Service, request: object, context: Context) -> Any:
        method = f"{self.service_name}/{name}"
        with self.telemetry.call(method, context) as call:
            try:
                return await handler(self, request, context)
            except Exception as error:
                failure = failure_for(error)
                report(failure, error, method=method)
                call.fail(failure.status, error, unexpected=failure.unexpected)
        await abort(context, failure, method=method)

    return cast(F, wrapper)


def peer_address(context: Context) -> tuple[str, int] | None:
    """The caller's ``(host, port)`` from the gRPC peer string
    (``ipv4:127.0.0.1:50051``, ``ipv6:[::1]:50051``); ``None`` for a peer
    that is not an IP address."""
    scheme, _, address = unquote(context.peer() or "").partition(":")
    host, _, port = address.rpartition(":")
    if scheme not in _IP_PEER_SCHEMES or not host or not port.isdigit():
        return None
    return host.strip("[]"), int(port)


def payload_hash(request: Message) -> str:
    return hashlib.sha256(request.SerializeToString(deterministic=True)).hexdigest()


def record_audit(
    state: AppState, context: Context, *, method: str, request: Message | None, outcome: str
) -> None:
    """One audit record for a write call: the method, the hash of the request
    and the caller's address. The RPC's name sets the activity (``api.audit``)."""
    state.audit.record(
        now=state.clock.now_utc(),
        account_id=state.expected_account_id,
        method=GRPC_METHOD,
        path=f"/{method}",
        payload_hash=payload_hash(request) if request is not None else None,
        outcome=outcome,
        client=peer_address(context),
    )
