"""BridgeWorker tracing (plan §84): the trace context crosses the thread
boundary, and work submitted outside a trace creates no span."""

import threading

import pytest
from opentelemetry import trace
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.trace import StatusCode

from mt5_bridge.core.tracing import span
from mt5_bridge.core.worker import (
    CALL_SPAN,
    DISPATCH_SPAN,
    BridgeWorker,
    CommandPriority,
    WorkerQueueSaturated,
    WorkerStopped,
)
from mt5_bridge.mt5.client import Mt5Client
from mt5_bridge.mt5.fake_client import FakeMt5Client

from testkit.fixtures import default_account, default_symbols
from testkit.telemetry import parent_id, span_id, span_named, trace_id


def make_worker(*, max_market_poll_backlog: int = 1000) -> BridgeWorker:
    client = FakeMt5Client(symbols=default_symbols(), account=default_account())
    return BridgeWorker(client, max_market_poll_backlog=max_market_poll_backlog)


def test_the_call_on_the_worker_thread_is_a_child_of_the_submitting_span(
    spans: InMemorySpanExporter,
) -> None:
    worker = make_worker()
    worker.start()
    seen: dict[str, object] = {}

    def read_terminal(client: Mt5Client) -> bool:
        seen["thread"] = threading.current_thread().name
        seen["span_id"] = trace.get_current_span().get_span_context().span_id
        return client.terminal_info().connected

    try:
        with span("request"):
            future = worker.submit(CommandPriority.ORDER, read_terminal)
            assert future.result(timeout=5) is False
    finally:
        worker.stop()

    finished = spans.get_finished_spans()
    request = span_named(finished, "request")
    dispatch = span_named(finished, DISPATCH_SPAN)
    call = span_named(finished, CALL_SPAN)
    assert parent_id(dispatch) == span_id(request)
    assert parent_id(call) == span_id(dispatch)
    assert trace_id(call) == trace_id(request)
    # The command really ran on the worker thread, inside the call span.
    assert seen == {"thread": "mt5-bridge-worker", "span_id": span_id(call)}
    assert dispatch.attributes is not None
    assert dispatch.attributes["mt5_bridge.worker.priority"] == "order"
    # A command is named after the function that built it.
    assert dispatch.attributes["mt5_bridge.worker.command"] == (
        "test_the_call_on_the_worker_thread_is_a_child_of_the_submitting_span"
    )
    assert call.attributes is not None
    assert isinstance(call.attributes["mt5_bridge.worker.queue_wait_seconds"], float)


def test_a_failing_command_sets_the_error_status_on_both_spans(
    spans: InMemorySpanExporter,
) -> None:
    worker = make_worker()

    def boom(_client: object) -> None:
        raise ValueError("boom")

    with span("request"):
        future = worker.submit(CommandPriority.ORDER, boom)
    worker.drain()

    with pytest.raises(ValueError, match="boom"):
        future.result()
    finished = spans.get_finished_spans()
    call = span_named(finished, CALL_SPAN)
    dispatch = span_named(finished, DISPATCH_SPAN)
    assert call.status.status_code is StatusCode.ERROR
    assert [event.name for event in call.events] == ["exception"]
    assert dispatch.status.status_code is StatusCode.ERROR
    assert dispatch.attributes is not None and dispatch.attributes["error.type"] == "ValueError"
    assert span_named(finished, "request").status.status_code is StatusCode.UNSET


def test_work_submitted_outside_a_trace_creates_no_span(spans: InMemorySpanExporter) -> None:
    worker = make_worker()

    future = worker.submit(CommandPriority.MARKET_POLL, lambda client: client.terminal_info())
    worker.drain()

    assert future.done()
    assert spans.get_finished_spans() == ()


def test_a_shed_command_ends_its_dispatch_span_as_failed(spans: InMemorySpanExporter) -> None:
    worker = make_worker(max_market_poll_backlog=0)

    with span("request"):
        shed = worker.submit(CommandPriority.MARKET_POLL, lambda _client: None)

    with pytest.raises(WorkerQueueSaturated):
        shed.result(timeout=1)
    dispatch = span_named(spans.get_finished_spans(), DISPATCH_SPAN)
    assert dispatch.status.status_code is StatusCode.ERROR
    assert dispatch.attributes is not None
    assert dispatch.attributes["error.type"] == "WorkerQueueSaturated"


def test_a_command_abandoned_at_stop_ends_its_dispatch_span(spans: InMemorySpanExporter) -> None:
    worker = make_worker()
    worker.start()
    worker.stop()

    with span("request"):
        late = worker.submit(CommandPriority.ORDER, lambda _client: None)

    with pytest.raises(WorkerStopped):
        late.result(timeout=1)
    dispatch = span_named(spans.get_finished_spans(), DISPATCH_SPAN)
    assert dispatch.attributes is not None
    assert dispatch.attributes["error.type"] == "WorkerStopped"
