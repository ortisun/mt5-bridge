"""The single worker thread that owns the terminal connection (plan §114).

The MT5 API is synchronous, single-connection, and callback-free: every
terminal call goes through this one thread via a priority command queue.
FastAPI's async handlers only ``submit`` a command and ``await`` its
``concurrent.futures.Future`` (bridged into asyncio with
``asyncio.wrap_future``); they never call the terminal directly. Priority
order is fixed: orders before execution polling before market polling.

Tracing (plan §84, docs/observability.md): a command submitted from inside a
trace gets two spans. ``mt5_bridge.worker.dispatch`` starts in ``submit`` on
the caller's thread and ends when the result is delivered, so it covers the
wait in the queue; ``mt5_bridge.mt5.call`` is its child, opened on the worker
thread around the command itself, which is how the trace context crosses the
thread boundary. A command submitted outside a trace -- every background
poll, every WebSocket market-data poll -- creates no span at all.
"""

import itertools
import queue
import threading
from collections.abc import Callable
from concurrent.futures import Future
from dataclasses import dataclass, field
from enum import IntEnum
from time import perf_counter
from typing import TypeVar, cast

from opentelemetry import trace
from opentelemetry.trace import Span

from mt5_bridge.core import tracing
from mt5_bridge.mt5.client import Mt5Client

T = TypeVar("T")

DISPATCH_SPAN = "mt5_bridge.worker.dispatch"
CALL_SPAN = "mt5_bridge.mt5.call"


class CommandPriority(IntEnum):
    """Lower runs first."""

    ORDER = 0
    EXECUTION_POLL = 1
    MARKET_POLL = 2


class WorkerStopped(Exception):
    """A command was still queued when ``stop()`` ran; it will never
    execute. Raised through the caller's awaited future instead of leaving
    it pending forever."""


class WorkerQueueSaturated(Exception):
    """The MARKET_POLL backlog was already at its cap when ``submit`` was
    called, so this command was shed rather than blocking the caller or
    queuing without limit (plan §114). Admission is bounded per priority,
    never by one shared cap: ORDER and EXECUTION_POLL are never shed by this
    — an order must never be dropped because market data backed up."""


def _stop_command(_client: Mt5Client) -> None:
    return None


def _command_name(command: Callable[[Mt5Client], object]) -> str:
    """The function that built the command: ``health`` for a lambda written
    inside ``health``, the function's own name otherwise."""
    qualified = getattr(command, "__qualname__", None) or type(command).__name__
    return str(qualified).split(".<locals>")[0]


def _start_dispatch_span(
    priority: CommandPriority, command: Callable[[Mt5Client], object]
) -> Span | None:
    if not tracing.in_trace():
        return None
    return tracing.start_span(
        DISPATCH_SPAN,
        attributes={
            "mt5_bridge.worker.priority": priority.name.lower(),
            "mt5_bridge.worker.command": _command_name(command),
        },
    )


def _end_failed(span: Span | None, error: Exception) -> None:
    if span is not None:
        tracing.record_error(span, error)
        span.end()


@dataclass(order=True)
class _QueueItem:
    priority: int
    sequence: int
    command: Callable[[Mt5Client], object] = field(compare=False)
    future: Future[object] = field(compare=False)
    dispatch_span: Span | None = field(compare=False, default=None)
    enqueued_at: float = field(compare=False, default_factory=perf_counter)


class BridgeWorker:
    """Admission and dispatch are deliberately separate concerns here.

    Dispatch always goes through one ``PriorityQueue`` (unbounded — a
    ``put`` on it must never itself raise or block) so ORDER strictly
    precedes EXECUTION_POLL precedes MARKET_POLL regardless of arrival
    order. Admission is bounded per priority via ``_market_poll_backlog``:
    only MARKET_POLL has a cap, so a backlog of market data can never shed,
    block, or even delay an ORDER or EXECUTION_POLL command — those are
    always accepted.
    """

    def __init__(self, client: Mt5Client, *, max_market_poll_backlog: int = 1000) -> None:
        self._client = client
        self._queue: queue.PriorityQueue[_QueueItem] = queue.PriorityQueue()
        self._max_market_poll_backlog = max_market_poll_backlog
        self._market_poll_backlog = 0
        self._backlog_lock = threading.Lock()
        self._counter = itertools.count()
        self._thread: threading.Thread | None = None
        self._stopped = False

    def submit(self, priority: CommandPriority, command: Callable[[Mt5Client], T]) -> Future[T]:
        """Never blocks the caller. Only MARKET_POLL can be shed (its
        backlog counter at cap fails this future immediately with
        ``WorkerQueueSaturated``); ORDER and EXECUTION_POLL are always
        admitted onto the (unbounded) dispatch queue. A command submitted
        after ``stop()`` has already run would otherwise sit in the queue
        forever with no thread left to drain (and fail) it — checked here
        instead, so it fails immediately with the same ``WorkerStopped`` a
        command still queued *when* ``stop()`` ran would get."""
        future: Future[object] = Future()
        dispatch_span = _start_dispatch_span(priority, command)
        if self._stopped:
            stopped = WorkerStopped("BridgeWorker is stopped")
            _end_failed(dispatch_span, stopped)
            future.set_exception(stopped)
            return cast(Future[T], future)
        if priority is CommandPriority.MARKET_POLL:
            with self._backlog_lock:
                if self._market_poll_backlog >= self._max_market_poll_backlog:
                    saturated = WorkerQueueSaturated(
                        f"MARKET_POLL backlog is at its cap "
                        f"({self._max_market_poll_backlog}); command dropped"
                    )
                    _end_failed(dispatch_span, saturated)
                    future.set_exception(saturated)
                    return cast(Future[T], future)
                self._market_poll_backlog += 1
        item = _QueueItem(
            priority=int(priority),
            sequence=next(self._counter),
            command=command,
            future=future,
            dispatch_span=dispatch_span,
        )
        self._queue.put(item)
        return cast(Future[T], future)

    def _process_one(self, item: _QueueItem) -> bool:
        """Runs ``item``. Returns ``False`` iff it was the stop marker."""
        if item.command is _stop_command:
            return False
        if item.priority == int(CommandPriority.MARKET_POLL):
            with self._backlog_lock:
                self._market_poll_backlog -= 1
        if not item.future.set_running_or_notify_cancel():
            if item.dispatch_span is not None:
                item.dispatch_span.end()
            return True
        try:
            result = self._execute(item)
        except Exception as error:  # noqa: BLE001 - delivered to the caller's Future, never lost
            item.future.set_exception(error)
        else:
            item.future.set_result(result)
        return True

    def _execute(self, item: _QueueItem) -> object:
        """Runs the command, inside ``CALL_SPAN`` when its submitter was in a
        trace. The dispatch span ends here, before the future resolves, so
        both spans are finished by the time the caller sees the result."""
        dispatch_span = item.dispatch_span
        if dispatch_span is None:
            return item.command(self._client)
        try:
            with tracing.span(
                CALL_SPAN,
                context=trace.set_span_in_context(dispatch_span),
                attributes={
                    "mt5_bridge.worker.queue_wait_seconds": perf_counter() - item.enqueued_at
                },
            ):
                return item.command(self._client)
        except Exception as error:
            tracing.mark_error(dispatch_span, error)
            raise
        finally:
            dispatch_span.end()

    def drain(self) -> int:
        """Synchronously process everything currently queued, in priority
        order. Used by tests and by any embedder that does not want a
        background thread; production wires ``start()`` instead."""
        processed = 0
        while True:
            try:
                item = self._queue.get_nowait()
            except queue.Empty:
                return processed
            if not self._process_one(item):
                return processed
            processed += 1

    def start(self) -> None:
        if self._thread is not None:
            raise RuntimeError("BridgeWorker is already started")
        self._stopped = False
        self._thread = threading.Thread(target=self._run, name="mt5-bridge-worker", daemon=True)
        self._thread.start()

    def _run(self) -> None:
        while True:
            item = self._queue.get()
            if not self._process_one(item):
                return

    def stop(self, *, timeout: float = 5.0) -> None:
        self._stopped = True
        if self._thread is None:
            return
        self._queue.put(
            _QueueItem(priority=-1, sequence=-1, command=_stop_command, future=Future())
        )
        self._thread.join(timeout=timeout)
        self._thread = None
        self._fail_abandoned_commands()

    def _fail_abandoned_commands(self) -> None:
        """Anything still queued once the thread is gone will never run —
        fail its future with ``WorkerStopped`` rather than leaving an
        ``await`` on the other end hanging forever."""
        while True:
            try:
                item = self._queue.get_nowait()
            except queue.Empty:
                return
            if item.command is _stop_command:
                continue
            if item.priority == int(CommandPriority.MARKET_POLL):
                with self._backlog_lock:
                    self._market_poll_backlog -= 1
            abandoned = WorkerStopped("BridgeWorker stopped before this command could run")
            _end_failed(item.dispatch_span, abandoned)
            if item.future.set_running_or_notify_cancel():
                item.future.set_exception(abandoned)
