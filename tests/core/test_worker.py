"""BridgeWorker: one thread, one command queue, orders before polling.

Ordering is tested by draining a queue that was never started on a thread
(fully deterministic, no timing); a single real-thread test only proves the
thread actually drives submitted work to completion.
"""

import threading

import pytest

from mt5_bridge.core.worker import (
    BridgeWorker,
    CommandPriority,
    WorkerQueueSaturated,
    WorkerStopped,
)
from mt5_bridge.mt5.fake_client import FakeMt5Client

from testkit.fixtures import default_account, default_symbols


def make_worker(*, max_market_poll_backlog: int = 1000) -> BridgeWorker:
    client = FakeMt5Client(symbols=default_symbols(), account=default_account())
    return BridgeWorker(client, max_market_poll_backlog=max_market_poll_backlog)


def test_orders_run_before_execution_polling_and_market_polling() -> None:
    worker = make_worker()
    order: list[str] = []
    worker.submit(CommandPriority.MARKET_POLL, lambda _client: order.append("market"))
    worker.submit(CommandPriority.EXECUTION_POLL, lambda _client: order.append("execution"))
    worker.submit(CommandPriority.ORDER, lambda _client: order.append("order"))

    worker.drain()

    assert order == ["order", "execution", "market"]


def test_same_priority_items_run_in_submission_order() -> None:
    worker = make_worker()
    order: list[str] = []
    worker.submit(CommandPriority.ORDER, lambda _client: order.append("first"))
    worker.submit(CommandPriority.ORDER, lambda _client: order.append("second"))

    worker.drain()

    assert order == ["first", "second"]


def test_submit_returns_a_future_resolved_with_the_command_result() -> None:
    worker = make_worker()
    future = worker.submit(CommandPriority.ORDER, lambda client: client.terminal_info())

    worker.drain()

    assert future.done()
    assert future.result().connected is False  # never connected in this test


def test_an_exception_in_the_command_is_delivered_through_the_future() -> None:
    worker = make_worker()

    def boom(_client: object) -> None:
        raise ValueError("boom")

    future = worker.submit(CommandPriority.ORDER, boom)
    worker.drain()

    assert future.done()
    with pytest.raises(ValueError, match="boom"):
        future.result()


def test_a_real_worker_thread_processes_submitted_work() -> None:
    worker = make_worker()
    worker.start()
    try:
        future = worker.submit(CommandPriority.ORDER, lambda client: client.terminal_info())
        result = future.result(timeout=5)
        assert result.connected is False
    finally:
        worker.stop()


def test_market_poll_is_shed_once_its_backlog_is_at_the_cap_instead_of_blocking() -> None:
    worker = make_worker(max_market_poll_backlog=2)
    worker.submit(CommandPriority.MARKET_POLL, lambda _client: 1)
    worker.submit(CommandPriority.MARKET_POLL, lambda _client: 2)

    shed = worker.submit(CommandPriority.MARKET_POLL, lambda _client: 3)

    with pytest.raises(WorkerQueueSaturated):
        shed.result(timeout=1)


def test_a_saturated_market_poll_backlog_never_sheds_or_blocks_an_order() -> None:
    # Admission is bounded per priority, not by one shared cap: an order
    # must never be dropped because market data backed up.
    worker = make_worker(max_market_poll_backlog=1)
    worker.submit(CommandPriority.MARKET_POLL, lambda _client: "fills the one slot")
    shed = worker.submit(CommandPriority.MARKET_POLL, lambda _client: "shed")
    with pytest.raises(WorkerQueueSaturated):
        shed.result(timeout=1)

    order_future = worker.submit(CommandPriority.ORDER, lambda _client: "accepted")
    execution_poll_future = worker.submit(
        CommandPriority.EXECUTION_POLL, lambda _client: "also accepted"
    )
    worker.drain()

    assert order_future.result(timeout=1) == "accepted"
    assert execution_poll_future.result(timeout=1) == "also accepted"


def test_the_market_poll_backlog_frees_up_as_items_are_processed() -> None:
    worker = make_worker(max_market_poll_backlog=1)
    worker.submit(CommandPriority.MARKET_POLL, lambda _client: "first")
    worker.drain()  # processed: the one slot is free again

    second = worker.submit(CommandPriority.MARKET_POLL, lambda _client: "second")
    worker.drain()

    assert second.result(timeout=1) == "second"


def test_stop_fails_commands_still_queued_rather_than_leaving_them_pending() -> None:
    worker = make_worker()
    worker.start()
    started = threading.Event()
    release = threading.Event()

    def blocking(_client: object) -> None:
        started.set()
        release.wait(timeout=5)

    running = worker.submit(CommandPriority.ORDER, blocking)
    assert started.wait(timeout=5), "the worker thread never picked up the blocking command"
    pending = worker.submit(CommandPriority.MARKET_POLL, lambda _client: "never runs")

    worker.stop(timeout=0.05)  # the thread is stuck in `blocking`; this join times out fast

    with pytest.raises(WorkerStopped):
        pending.result(timeout=1)

    release.set()  # let the real, now-orphaned thread finish instead of leaking blocked forever
    assert running.result(timeout=5) is None


def test_submitting_to_an_already_stopped_worker_fails_immediately() -> None:
    worker = make_worker()
    worker.start()
    worker.stop()

    future = worker.submit(CommandPriority.ORDER, lambda _client: "never runs")

    with pytest.raises(WorkerStopped):
        future.result(timeout=1)
