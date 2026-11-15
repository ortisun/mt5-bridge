"""KillSwitch: persistent, escalation-only, explicit-rearm-only state."""

import sqlite3
import threading
from collections.abc import Callable, Generator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path

from mt5_bridge.core.database import BridgeDatabase
from mt5_bridge.core.kill_switch import KillSwitch, KillSwitchAction, KillSwitchState
from mt5_bridge.core.metrics_port import NullMetrics

NOW = datetime(2024, 1, 1, tzinfo=UTC)
LATER = datetime(2024, 1, 1, 0, 1, tzinfo=UTC)


class SpyMetrics(NullMetrics):
    def __init__(self) -> None:
        self.increments: list[tuple[str, dict[str, object]]] = []

    def increment(self, name: str, value: int = 1, /, **attributes: object) -> None:
        self.increments.append((name, attributes))


def test_a_fresh_kill_switch_is_normal_and_does_not_block() -> None:
    switch = KillSwitch(BridgeDatabase(":memory:"), now=NOW, metrics=NullMetrics())

    status = switch.status()

    assert status.state is KillSwitchState.NORMAL
    assert switch.blocks_new_orders() is False


def test_halt_new_orders_blocks_new_orders() -> None:
    switch = KillSwitch(BridgeDatabase(":memory:"), now=NOW, metrics=NullMetrics())

    switch.activate(KillSwitchAction.HALT_NEW_ORDERS, reason="manual", now=NOW)

    assert switch.blocks_new_orders() is True
    assert switch.status().state is KillSwitchState.HALTED


def test_cancel_all_escalates_past_halt() -> None:
    switch = KillSwitch(BridgeDatabase(":memory:"), now=NOW, metrics=NullMetrics())
    switch.activate(KillSwitchAction.HALT_NEW_ORDERS, reason="manual", now=NOW)

    switch.activate(KillSwitchAction.CANCEL_ALL, reason="drawdown", now=LATER)

    assert switch.status().state is KillSwitchState.CANCELED_ALL


def test_activating_a_lower_action_after_flatten_does_not_downgrade() -> None:
    switch = KillSwitch(BridgeDatabase(":memory:"), now=NOW, metrics=NullMetrics())
    switch.activate(KillSwitchAction.FLATTEN, reason="loss limit", now=NOW)

    switch.activate(KillSwitchAction.HALT_NEW_ORDERS, reason="manual", now=LATER)

    assert switch.status().state is KillSwitchState.FLATTENED


def test_rearm_returns_to_normal() -> None:
    switch = KillSwitch(BridgeDatabase(":memory:"), now=NOW, metrics=NullMetrics())
    switch.activate(KillSwitchAction.FLATTEN, reason="loss limit", now=NOW)

    switch.rearm(now=LATER)

    status = switch.status()
    assert status.state is KillSwitchState.NORMAL
    assert status.reason is None


def test_state_persists_across_a_new_instance_against_the_same_database(tmp_path: Path) -> None:
    path = str(tmp_path / "bridge.db")
    db = BridgeDatabase(path)
    KillSwitch(db, now=NOW, metrics=NullMetrics()).activate(
        KillSwitchAction.CANCEL_ALL, reason="restart-test", now=NOW
    )
    db.close()

    reopened = KillSwitch(BridgeDatabase(path), now=LATER, metrics=NullMetrics())

    assert reopened.status().state is KillSwitchState.CANCELED_ALL


def test_activate_records_one_transition_metric_with_from_and_to_state() -> None:
    metrics = SpyMetrics()
    switch = KillSwitch(BridgeDatabase(":memory:"), now=NOW, metrics=metrics)

    switch.activate(KillSwitchAction.HALT_NEW_ORDERS, reason="manual", now=NOW)

    assert metrics.increments == [
        ("mt5_bridge.kill_switch.transition", {"from_state": "NORMAL", "to_state": "HALTED"})
    ]


def test_activate_records_nothing_when_the_action_does_not_escalate() -> None:
    metrics = SpyMetrics()
    switch = KillSwitch(BridgeDatabase(":memory:"), now=NOW, metrics=metrics)
    switch.activate(KillSwitchAction.FLATTEN, reason="loss limit", now=NOW)
    metrics.increments.clear()

    switch.activate(KillSwitchAction.HALT_NEW_ORDERS, reason="manual", now=LATER)

    assert metrics.increments == []


def test_rearm_records_a_transition_back_to_normal() -> None:
    metrics = SpyMetrics()
    switch = KillSwitch(BridgeDatabase(":memory:"), now=NOW, metrics=metrics)
    switch.activate(KillSwitchAction.FLATTEN, reason="loss limit", now=NOW)
    metrics.increments.clear()

    switch.rearm(now=LATER)

    assert metrics.increments == [
        ("mt5_bridge.kill_switch.transition", {"from_state": "FLATTENED", "to_state": "NORMAL"})
    ]


def test_rearm_records_nothing_when_already_normal() -> None:
    metrics = SpyMetrics()
    switch = KillSwitch(BridgeDatabase(":memory:"), now=NOW, metrics=metrics)

    switch.rearm(now=LATER)

    assert metrics.increments == []


class _RacingDatabase(BridgeDatabase):
    """Runs ``competitor`` once, just before the next transaction takes the lock.

    For an ``activate`` that reads the state and then opens a transaction to write
    its decision, that is the instant where a second caller (another thread) can
    escalate the switch; for one that decides inside the transaction, it is just
    an earlier caller.
    """

    def __init__(self, path: str) -> None:
        super().__init__(path)
        self.competitor: Callable[[], None] | None = None

    @contextmanager
    def transaction(self) -> Generator[sqlite3.Connection]:
        competitor, self.competitor = self.competitor, None
        if competitor is not None:
            competitor()
        with super().transaction() as conn:
            yield conn


def test_an_activation_that_raced_a_higher_one_does_not_downgrade_it() -> None:
    metrics = SpyMetrics()
    db = _RacingDatabase(":memory:")
    switch = KillSwitch(db, now=NOW, metrics=metrics)

    def flatten() -> None:
        switch.activate(KillSwitchAction.FLATTEN, reason="loss limit", now=NOW)

    db.competitor = flatten

    status = switch.activate(KillSwitchAction.HALT_NEW_ORDERS, reason="manual", now=LATER)

    assert switch.status().state is KillSwitchState.FLATTENED
    assert status.state is KillSwitchState.FLATTENED
    assert status.reason == "loss limit"
    assert metrics.increments == [
        ("mt5_bridge.kill_switch.transition", {"from_state": "NORMAL", "to_state": "FLATTENED"})
    ]


def test_concurrent_activations_from_threads_end_at_the_highest_severity() -> None:
    switch = KillSwitch(BridgeDatabase(":memory:"), now=NOW, metrics=NullMetrics())
    actions = [
        KillSwitchAction.HALT_NEW_ORDERS,
        KillSwitchAction.CANCEL_ALL,
        KillSwitchAction.FLATTEN,
        KillSwitchAction.HALT_NEW_ORDERS,
    ]
    start = threading.Barrier(len(actions))

    def activate(action: KillSwitchAction) -> None:
        start.wait()
        for _ in range(50):
            switch.activate(action, reason=action.value, now=LATER)

    threads = [threading.Thread(target=activate, args=(action,)) for action in actions]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    status = switch.status()
    assert status.state is KillSwitchState.FLATTENED
    assert status.reason == KillSwitchAction.FLATTEN.value


class _InterleavingDatabase(BridgeDatabase):
    """Runs ``before_write`` once, right before the next kill switch UPDATE executes.

    ``before_write`` stands for another process, or another handle, on the same
    file: it lands after whatever this handle read and before what it writes.
    """

    def __init__(self, path: str) -> None:
        super().__init__(path)
        self.before_write: Callable[[], None] | None = None

    @contextmanager
    def transaction(self) -> Generator[sqlite3.Connection]:
        with super().transaction() as conn:
            conn.set_trace_callback(self._trace)
            try:
                yield conn
            finally:
                conn.set_trace_callback(None)

    def _trace(self, statement: str) -> None:
        if self.before_write is not None and statement.startswith("UPDATE kill_switch_state"):
            before_write, self.before_write = self.before_write, None
            before_write()


def test_a_stale_read_on_one_handle_does_not_overwrite_a_higher_state_from_another(
    tmp_path: Path,
) -> None:
    path = str(tmp_path / "bridge.db")
    metrics = SpyMetrics()
    handle_a = _InterleavingDatabase(path)
    switch_a = KillSwitch(handle_a, now=NOW, metrics=metrics)
    switch_b = KillSwitch(BridgeDatabase(path), now=NOW, metrics=NullMetrics())

    def flatten_from_the_other_handle() -> None:
        switch_b.activate(KillSwitchAction.FLATTEN, reason="loss limit", now=NOW)

    handle_a.before_write = flatten_from_the_other_handle

    status = switch_a.activate(KillSwitchAction.HALT_NEW_ORDERS, reason="manual", now=LATER)

    stored = KillSwitch(BridgeDatabase(path), now=LATER, metrics=NullMetrics()).status()
    assert stored.state is KillSwitchState.FLATTENED
    assert stored.reason == "loss limit"
    assert stored.updated_at == NOW
    assert status == stored


def test_a_stale_read_on_one_handle_does_not_stop_it_escalating_past_the_other(
    tmp_path: Path,
) -> None:
    path = str(tmp_path / "bridge.db")
    handle_a = _InterleavingDatabase(path)
    switch_a = KillSwitch(handle_a, now=NOW, metrics=NullMetrics())
    switch_b = KillSwitch(BridgeDatabase(path), now=NOW, metrics=NullMetrics())

    def halt_from_the_other_handle() -> None:
        switch_b.activate(KillSwitchAction.HALT_NEW_ORDERS, reason="manual", now=NOW)

    handle_a.before_write = halt_from_the_other_handle

    status = switch_a.activate(KillSwitchAction.FLATTEN, reason="loss limit", now=LATER)

    stored = KillSwitch(BridgeDatabase(path), now=LATER, metrics=NullMetrics()).status()
    assert stored.state is KillSwitchState.FLATTENED
    assert stored.reason == "loss limit"
    assert stored.updated_at == LATER
    assert status == stored


def test_an_activation_that_does_not_escalate_writes_nothing() -> None:
    switch = KillSwitch(BridgeDatabase(":memory:"), now=NOW, metrics=NullMetrics())
    switch.activate(KillSwitchAction.FLATTEN, reason="loss limit", now=NOW)

    halted = switch.activate(KillSwitchAction.HALT_NEW_ORDERS, reason="manual", now=LATER)
    flattened_again = switch.activate(
        KillSwitchAction.FLATTEN, reason="again", now=LATER + (LATER - NOW)
    )

    for status in (halted, flattened_again, switch.status()):
        assert status.state is KillSwitchState.FLATTENED
        assert status.reason == "loss limit"
        assert status.updated_at == NOW
