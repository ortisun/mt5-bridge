"""Span helpers for instrumented bridge code (plan §84).

This module is the only thing a call site needs to create a span; it depends
on the OpenTelemetry *API* alone, so importing it costs nothing and every
helper is a no-op until ``core.observability`` installs a real
``TracerProvider`` for the process. The SDK, the exporters and their
configuration live in ``core.observability``.

Two rules every helper here enforces (plan §84: "logs and spans undergo
secret redaction"):

- an exception reaches a span only through ``record_error``, which scrubs the
  process's known secret values (``register_span_secrets``) from the message
  and the stack trace -- the SDK's own automatic exception recording is
  switched off on every span started here;
- attributes are chosen by the call site from non-secret identifiers
  (symbol, client order id, order state); no helper copies request headers,
  bodies or account credentials into a span.
"""

from __future__ import annotations

import functools
import inspect
import traceback
from collections.abc import Callable, Coroutine, Generator, Iterable, Mapping, Sequence
from contextlib import contextmanager
from enum import Enum
from typing import Any

from opentelemetry import trace
from opentelemetry.context import Context
from opentelemetry.trace import Span, SpanKind, Status, StatusCode

TRACER_NAME = "mt5_bridge"

AttributeValue = str | bool | int | float
Attributes = Mapping[str, AttributeValue]

# A proxy until ``core.observability`` installs the process-wide provider;
# it resolves to the real tracer on first use after that.
_TRACER = trace.get_tracer(TRACER_NAME)

_secrets: frozenset[str] = frozenset()
_REDACTED = "[redacted]"


def register_span_secrets(secrets: Iterable[str]) -> None:
    """Add literal secret values that ``record_error`` must scrub from spans."""
    global _secrets  # noqa: PLW0603 - one process-wide secret set, as in core.log_redaction
    _secrets = _secrets | frozenset(value for value in secrets if value)


def scrub(text: str) -> str:
    for secret in _secrets:
        if secret in text:
            text = text.replace(secret, _REDACTED)
    return text


def error_type(error: BaseException) -> str:
    return type(error).__qualname__


def mark_error(span: Span, error: BaseException) -> None:
    """Set the error status and ``error.type`` without adding an exception event."""
    name = error_type(error)
    span.set_attribute("error.type", name)
    span.set_status(Status(StatusCode.ERROR, name))


def record_error(span: Span, error: BaseException) -> None:
    """Record ``error`` as a scrubbed ``exception`` event and mark the span failed."""
    if not span.is_recording():
        return
    span.add_event(
        "exception",
        {
            "exception.type": error_type(error),
            "exception.message": scrub(str(error)),
            "exception.stacktrace": scrub("".join(traceback.format_exception(error))),
        },
    )
    mark_error(span, error)


def in_trace() -> bool:
    """Whether the caller runs inside a trace (sampled or not)."""
    return trace.get_current_span().get_span_context().is_valid


def start_span(
    name: str,
    *,
    kind: SpanKind = SpanKind.INTERNAL,
    attributes: Attributes | None = None,
    context: Context | None = None,
) -> Span:
    """Start a span without making it current; the caller must ``end()`` it."""
    return _TRACER.start_span(name, context=context, kind=kind, attributes=attributes)


@contextmanager
def span(
    name: str,
    *,
    kind: SpanKind = SpanKind.INTERNAL,
    attributes: Attributes | None = None,
    context: Context | None = None,
) -> Generator[Span]:
    """Run a block inside a current span; an escaping exception is recorded
    (scrubbed) and re-raised, and the span always ends."""
    with _TRACER.start_as_current_span(
        name,
        context=context,
        kind=kind,
        attributes=attributes,
        record_exception=False,
        set_status_on_exception=False,
    ) as current:
        try:
            yield current
        except Exception as error:
            record_error(current, error)
            raise


def _attribute_value(value: object) -> AttributeValue | None:
    if isinstance(value, Enum):
        value = value.value
    if value is None:
        return None
    if isinstance(value, str | bool | int | float):
        return value
    return str(value)


def _captured(
    signature: inspect.Signature,
    capture: Sequence[str],
    prefix: str,
    args: tuple[object, ...],
    kwargs: dict[str, object],
) -> dict[str, AttributeValue]:
    if not capture:
        return {}
    arguments = signature.bind_partial(*args, **kwargs).arguments
    attributes: dict[str, AttributeValue] = {}
    for name in capture:
        value = _attribute_value(arguments.get(name))
        if value is not None:
            attributes[f"{prefix}{name}"] = value
    return attributes


def traced(name: str, *, capture: Sequence[str] = (), prefix: str = "mt5_bridge."):
    """Decorate a coroutine function so each call runs inside a span ``name``.

    ``capture`` names arguments of the decorated function copied to span
    attributes as ``<prefix><argument>`` (enums by value, ``None`` omitted).
    List only non-secret identifiers there.
    """

    def decorate[**P, R](
        function: Callable[P, Coroutine[Any, Any, R]],
    ) -> Callable[P, Coroutine[Any, Any, R]]:
        signature = inspect.signature(function)

        @functools.wraps(function)
        async def wrapper(*args: P.args, **kwargs: P.kwargs) -> R:
            attributes = _captured(signature, capture, prefix, args, kwargs)
            with span(name, attributes=attributes):
                return await function(*args, **kwargs)

        return wrapper

    return decorate


def traced_sync(name: str, *, capture: Sequence[str] = (), prefix: str = "mt5_bridge."):
    """``traced`` for a plain (synchronous) function."""

    def decorate[**P, R](function: Callable[P, R]) -> Callable[P, R]:
        signature = inspect.signature(function)

        @functools.wraps(function)
        def wrapper(*args: P.args, **kwargs: P.kwargs) -> R:
            attributes = _captured(signature, capture, prefix, args, kwargs)
            with span(name, attributes=attributes):
                return function(*args, **kwargs)

        return wrapper

    return decorate


__all__ = [
    "TRACER_NAME",
    "AttributeValue",
    "Attributes",
    "error_type",
    "in_trace",
    "mark_error",
    "record_error",
    "register_span_secrets",
    "scrub",
    "span",
    "start_span",
    "traced",
    "traced_sync",
]
