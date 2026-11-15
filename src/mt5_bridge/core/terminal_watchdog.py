"""Terminal connection watchdog: detect a disconnected terminal and
re-initialize with bounded backoff (plan §114 UTM VM Operations: "the
bridge detects a stopped/disconnected terminal (``terminal_info().connected``),
attempts ``initialize`` with backoff, publishes ``status``, and never
assumes order state during the outage").

Before this module, the terminal connected exactly once, at boot
(``main.build_mt5_client``); nothing detected a later drop. This mirrors
``core.clock_drift``'s split on purpose: a pure, mutable state object plus a
pure transition function (unit-tested with a fake terminal and no real
timer), with the periodic-loop wiring living in ``api/background.py`` and
the shared state read by both ``OrderService`` (to block new submissions
while disconnected) and the WS ``status`` channel/health route (to report
it) — the same "one shared holder, several readers" shape
``ClockDriftState`` already uses.
"""

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Protocol

from mt5_bridge.core.metrics_port import Metrics
from mt5_bridge.mt5.client import Mt5Client


class TerminalReconnect(Protocol):
    def __call__(self, client: Mt5Client) -> None:
        """Re-run the same ``connect``/``attach`` procedure boot used."""
        ...


@dataclass(frozen=True, slots=True)
class WatchdogBackoffPolicy:
    base_seconds: float
    max_seconds: float

    def __post_init__(self) -> None:
        if not 0 < self.base_seconds <= self.max_seconds:
            raise ValueError("watchdog backoff requires 0 < base <= max")

    def delay(self, attempt: int) -> timedelta:
        """``attempt`` is 1 for the first failed reconnection attempt."""
        seconds = min(self.base_seconds * (2 ** max(attempt - 1, 0)), self.max_seconds)
        return timedelta(seconds=seconds)


@dataclass
class TerminalWatchdogState:
    """Shared, mutable last-known connectivity. Written only by
    ``watchdog_tick``; read by ``OrderService`` (rejects new submissions
    while ``connected`` is false) and by the health route/``status``
    channel (to report it)."""

    connected: bool = True
    attempt: int = 0
    next_attempt_at: datetime | None = None


def watchdog_tick(
    state: TerminalWatchdogState,
    client: Mt5Client,
    *,
    reconnect: TerminalReconnect,
    now: datetime,
    backoff: WatchdogBackoffPolicy,
    metrics: Metrics,
) -> bool:
    """One watchdog cycle. Never raises: a probe or reconnect failure keeps
    the terminal marked disconnected and schedules the next bounded-backoff
    attempt instead of propagating one bad cycle into the caller's loop.

    Returns whether ``state.connected`` changed during this call, so the
    caller publishes a ``status`` update exactly on a transition rather
    than every cycle. Every disconnect transition increments plan §84's
    stream-gap counter; every reconnect transition increments its
    reconnect counter.
    """
    previous = state.connected
    try:
        state.connected = bool(client.terminal_info().connected)
    except Exception:  # noqa: BLE001 - any terminal-call failure means "not connected"
        state.connected = False
    if state.connected:
        state.attempt = 0
        state.next_attempt_at = None
        _record_transition(metrics, previous=previous, connected=state.connected)
        return previous != state.connected
    if state.next_attempt_at is None or now >= state.next_attempt_at:
        try:
            reconnect(client)
            state.connected = bool(client.terminal_info().connected)
        except Exception:  # noqa: BLE001 - a failed reconnect stays disconnected
            state.connected = False
        if state.connected:
            state.attempt = 0
            state.next_attempt_at = None
        else:
            state.attempt += 1
            state.next_attempt_at = now + backoff.delay(state.attempt)
    _record_transition(metrics, previous=previous, connected=state.connected)
    return previous != state.connected


def _record_transition(metrics: Metrics, *, previous: bool, connected: bool) -> None:
    if previous == connected:
        return
    if connected:
        metrics.increment("mt5_bridge.stream.reconnect")
    else:
        metrics.increment("mt5_bridge.stream.gap")


__all__ = [
    "TerminalReconnect",
    "TerminalWatchdogState",
    "WatchdogBackoffPolicy",
    "watchdog_tick",
]
