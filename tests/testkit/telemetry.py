"""In-memory span capture for tests (no exporter, no network).

Instrumented code takes its tracer from the process-wide OpenTelemetry
provider, which can be set only once per process. ``capture_spans`` sets it
on first use to a provider that hands every finished span, synchronously, to
one in-memory exporter, and empties that exporter on each call. No bridge
code path a test runs registers a global provider (only ``main()`` does), so
this one always wins.
"""

from collections.abc import Sequence

from opentelemetry import trace
from opentelemetry.sdk.trace import ReadableSpan, TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

TRACE_ID = 0x0AF7651916CD43DD8448EB211C80319C
PARENT_SPAN_ID = 0xB7AD6B7169203331
TRACEPARENT = f"00-{TRACE_ID:032x}-{PARENT_SPAN_ID:016x}-01"


class _Capture:
    exporter = InMemorySpanExporter()
    provider: TracerProvider | None = None


def capture_spans() -> InMemorySpanExporter:
    if _Capture.provider is None:
        provider = TracerProvider()
        provider.add_span_processor(SimpleSpanProcessor(_Capture.exporter))
        trace.set_tracer_provider(provider)
        assert trace.get_tracer_provider() is provider, "another global TracerProvider was set"
        _Capture.provider = provider
    _Capture.exporter.clear()
    return _Capture.exporter


def span_named(spans: Sequence[ReadableSpan], name: str) -> ReadableSpan:
    matches = [span for span in spans if span.name == name]
    assert len(matches) == 1, f"expected one span {name!r}, got {[s.name for s in spans]}"
    return matches[0]


def parent_id(span: ReadableSpan) -> int | None:
    return span.parent.span_id if span.parent is not None else None


def span_id(span: ReadableSpan) -> int:
    context = span.get_span_context()
    assert context is not None
    return context.span_id


def trace_id(span: ReadableSpan) -> int:
    context = span.get_span_context()
    assert context is not None
    return context.trace_id
