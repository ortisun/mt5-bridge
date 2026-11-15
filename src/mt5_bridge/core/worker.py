"""The single worker thread that owns the terminal connection (plan §114).

The MT5 API is synchronous, single-connection, and callback-free: every
terminal call goes through this one thread via a priority command queue.
FastAPI's async handlers only ``submit`` a command and ``await`` its
``concurrent.futures.Future`` (bridged into asyncio with
``asyncio.wrap_future``); they never call the terminal directly. Priority
order is fixed: orders before execution polling before market polling.
"""

import itertools
import queue
import threading
from collections.abc import Callable
from concurrent.futures import Future
from dataclasses import dataclass, field
from enum import IntEnum
from typing import TypeVar, cast

from mt5_bridge.mt5.client import Mt5Client

T = TypeVar("T")


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


@dataclass(order=True)
class _QueueItem:
    priority: int
    sequence: int
    command: Callable[[Mt5Client], object] = field(compare=False)
    future: Future[object] = field(compare=False)


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
        if self._stopped:
            future.set_exception(WorkerStopped("BridgeWorker is stopped"))
            return cast(Future[T], future)
        if priority is CommandPriority.MARKET_POLL:
            with self._backlog_lock:
                if self._market_poll_backlog >= self._max_market_poll_backlog:
                    future.set_exception(
                        WorkerQueueSaturated(
                            f"MARKET_POLL backlog is at its cap "
                            f"({self._max_market_poll_backlog}); command dropped"
                        )
                    )
                    return cast(Future[T], future)
                self._market_poll_backlog += 1
        item = _QueueItem(
            priority=int(priority), sequence=next(self._counter), command=command, future=future
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
            return True
        try:
            result = item.command(self._client)
        except Exception as error:  # noqa: BLE001 - delivered to the caller's Future, never lost
            item.future.set_exception(error)
        else:
            item.future.set_result(result)
        return True

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
            if item.future.set_running_or_notify_cancel():
                item.future.set_exception(
                    WorkerStopped("BridgeWorker stopped before this command could run")
                )
