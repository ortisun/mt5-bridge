"""Span helpers (plan §84): a span per call, scrubbed errors, captured arguments."""

from enum import StrEnum

import pytest
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.trace import StatusCode

from mt5_bridge.core.tracing import (
    in_trace,
    register_span_secrets,
    span,
    start_span,
    traced,
    traced_sync,
)

from testkit.telemetry import parent_id, span_id, span_named


class Side(StrEnum):
    BUY = "BUY"


def test_a_span_block_is_current_inside_and_finished_after(spans: InMemorySpanExporter) -> None:
    assert not in_trace()
    with span("outer", attributes={"k": "v"}):
        assert in_trace()
        with span("inner"):
            pass

    finished = spans.get_finished_spans()
    outer, inner = span_named(finished, "outer"), span_named(finished, "inner")
    assert parent_id(inner) == span_id(outer)
    assert outer.attributes is not None and outer.attributes["k"] == "v"
    assert outer.status.status_code is StatusCode.UNSET


def test_an_escaping_exception_sets_the_error_status_and_is_re_raised(
    spans: InMemorySpanExporter,
) -> None:
    with pytest.raises(ValueError, match="boom"), span("failing"):
        raise ValueError("boom")

    failed = span_named(spans.get_finished_spans(), "failing")
    assert failed.status.status_code is StatusCode.ERROR
    assert failed.attributes is not None and failed.attributes["error.type"] == "ValueError"
    [event] = failed.events
    assert event.name == "exception"
    assert event.attributes is not None
    assert event.attributes["exception.type"] == "ValueError"
    assert event.attributes["exception.message"] == "boom"


def test_a_registered_secret_never_reaches_a_span(spans: InMemorySpanExporter) -> None:
    register_span_secrets({"tok-span-secret"})

    with pytest.raises(RuntimeError), span("failing"):
        raise RuntimeError("login failed with tok-span-secret")

    [event] = span_named(spans.get_finished_spans(), "failing").events
    assert event.attributes is not None
    assert event.attributes["exception.message"] == "login failed with [redacted]"
    assert "tok-span-secret" not in str(event.attributes["exception.stacktrace"])


def test_start_span_does_not_become_the_current_span(spans: InMemorySpanExporter) -> None:
    detached = start_span("detached")
    assert not in_trace()
    detached.end()

    assert span_named(spans.get_finished_spans(), "detached").parent is None


@pytest.mark.asyncio
async def test_traced_captures_the_named_arguments_only(spans: InMemorySpanExporter) -> None:
    @traced("op", capture=("order_id", "side", "missing"), prefix="x.")
    async def operation(
        order_id: str, *, side: Side, volume: int, missing: str | None = None
    ) -> int:
        del order_id, side, missing
        return volume

    assert await operation("co-1", side=Side.BUY, volume=3) == 3

    attributes = span_named(spans.get_finished_spans(), "op").attributes
    assert attributes == {"x.order_id": "co-1", "x.side": "BUY"}


def test_traced_sync_records_a_failure(spans: InMemorySpanExporter) -> None:
    @traced_sync("sync-op")
    def operation() -> None:
        raise KeyError("nope")

    with pytest.raises(KeyError):
        operation()

    assert span_named(spans.get_finished_spans(), "sync-op").status.status_code is StatusCode.ERROR
