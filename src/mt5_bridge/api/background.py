"""Periodic safety loops, wired into the running process (plan §107, §114).

Each ``*_once`` function is one iteration's worth of work and is directly
callable — that is deliberate: an end-to-end test drives the real FastAPI
app (ASGI client, ``FakeMt5Client``, an injected ``FixedClock``) and then
calls e.g. ``poll_executions_once(state)`` once instead of waiting on a real
timer, which would either be slow or need sleep-based polling (both against
this project's testing rules). ``start_background_loops`` is the actual
production scheduler: it is what ``main.py`` starts (never a test), driving
every ``*_once`` function on a plain ``asyncio.sleep`` cadence read from
``BridgeSettings``.

``sweep_dead_man_once`` deliberately does not reuse
``core.supervisor.sweep_dead_man_switch`` (kept as-is, and still unit-tested,
as a pure reference implementation operating on a raw ``Mt5Client``).
Wiring the *running* sweep through ``OrderService.cancel_all()`` instead
keeps every cancellation on the worker thread and in the journal, rather
than a periodic loop reaching around the worker to touch the terminal
client directly.
"""

import asyncio
import contextlib
import functools
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from mt5_bridge.api.app_state import AppState
from mt5_bridge.api.run_on_worker import run_on_worker
from mt5_bridge.core.clock_drift import ClockDriftProbe, measure_drift_into
from mt5_bridge.core.kill_switch import KillSwitchAction
from mt5_bridge.core.terminal_watchdog import WatchdogBackoffPolicy, watchdog_tick
from mt5_bridge.core.worker import CommandPriority

logger = logging.getLogger("mt5_bridge")


async def poll_executions_once(state: AppState) -> tuple[str, ...]:
    """One ``ExecutionPoller`` cycle, run on the worker thread like every
    other terminal call, then timestamped into ``HealthState`` so ``/v1/health``
    can report how long ago the poller last actually ran."""
    affected = await run_on_worker(
        state.worker,
        CommandPriority.EXECUTION_POLL,
        lambda client: state.execution_poller.poll_once(client, state.clock),
    )
    state.health.last_execution_poll_at = state.clock.now_utc()
    return affected


async def sweep_dead_man_once(state: AppState) -> bool:
    """Block first, clean up second: activating the kill switch before the
    cancellation closes the window where a new order could still slip in
    between "lease expired" and "trading halted". Returns whether the lease
    had in fact expired (and a sweep therefore ran)."""
    if not state.dead_man_switch.is_expired(state.clock.now_ms()):
        return False
    state.kill_switch.activate(
        KillSwitchAction.CANCEL_ALL,
        reason="dead-man switch lease expired",
        now=state.clock.now_utc(),
    )
    await state.order_service.cancel_all()
    # Only a completed cancellation consumes the expiry: if cancel_all raised
    # (e.g. the terminal dropped too), the lease stays expired and the next
    # sweep retries instead of silently leaving orders resting.
    state.dead_man_switch.disarm()
    return True


async def measure_clock_drift_once(state: AppState, *, probe: ClockDriftProbe) -> None:
    """Runs ``probe`` off the event loop thread: production's probe is a
    blocking UDP round trip, which must never stall request handling."""
    loop = asyncio.get_running_loop()
    await loop.run_in_executor(
        None, functools.partial(measure_drift_into, state.clock_drift_state, probe=probe)
    )


async def watch_terminal_once(state: AppState, *, backoff: WatchdogBackoffPolicy) -> bool:
    """One terminal-watchdog cycle (plan §114 UTM VM Operations: detect a
    dropped terminal, attempt ``initialize`` with bounded backoff), run on
    the worker thread like every other terminal call. Returns whether
    ``state.terminal_watchdog_state.connected`` changed, so the WS ``status``
    channel publishes exactly on a transition rather than every cycle.
    """
    now = state.clock.now_utc()
    return await run_on_worker(
        state.worker,
        CommandPriority.MARKET_POLL,
        lambda client: watchdog_tick(
            state.terminal_watchdog_state,
            client,
            reconnect=state.terminal_reconnect,
            now=now,
            backoff=backoff,
            metrics=state.metrics,
        ),
    )


async def snapshot_equity_once(state: AppState) -> None:
    """Feeds ``AccountEquityTracker`` so ``LimitsGuard``'s daily-loss rule has
    a real, moving account equity to compare against instead of a fixed
    zero."""
    account = await run_on_worker(
        state.worker, CommandPriority.MARKET_POLL, lambda client: client.account_info()
    )
    state.equity_tracker.record(account.equity, now=state.clock.now_utc())


@dataclass(frozen=True, slots=True)
class BackgroundLoops:
    """A handle on every loop task ``start_background_loops`` started, so
    the ASGI lifespan can cancel and await them cleanly on shutdown."""

    tasks: tuple[asyncio.Task[None], ...]

    async def stop(self) -> None:
        for task in self.tasks:
            task.cancel()
        for task in self.tasks:
            with contextlib.suppress(asyncio.CancelledError):
                await task


async def _loop(name: str, interval_seconds: float, body: Callable[[], Awaitable[object]]) -> None:
    while True:
        try:
            await body()
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - one bad iteration must never kill the loop
            logger.exception("mt5-bridge background loop %r: iteration failed", name)
        await asyncio.sleep(interval_seconds)


def start_background_loops(state: AppState, *, drift_probe: ClockDriftProbe) -> BackgroundLoops:
    """Production-only (``main.py``): schedules every periodic safety loop
    at the interval configured in ``BridgeSettings``. Never started by
    ``create_app`` itself — tests call the ``*_once`` functions directly
    instead, deterministically."""
    settings = state.settings
    specs: tuple[tuple[str, float, Callable[[], Awaitable[object]]], ...] = (
        (
            "execution-poll",
            settings.execution_poll_interval_seconds,
            lambda: poll_executions_once(state),
        ),
        (
            "dead-man-sweep",
            settings.dead_man_sweep_interval_seconds,
            lambda: sweep_dead_man_once(state),
        ),
        (
            "clock-drift",
            settings.clock_drift_probe_interval_seconds,
            lambda: measure_clock_drift_once(state, probe=drift_probe),
        ),
        (
            "equity-snapshot",
            settings.equity_snapshot_interval_seconds,
            lambda: snapshot_equity_once(state),
        ),
        (
            "terminal-watchdog",
            settings.watchdog_poll_interval_seconds,
            lambda: watch_terminal_once(
                state,
                backoff=WatchdogBackoffPolicy(
                    base_seconds=settings.watchdog_backoff_base_seconds,
                    max_seconds=settings.watchdog_backoff_max_seconds,
                ),
            ),
        ),
    )
    tasks = tuple(
        asyncio.create_task(_loop(name, interval, body), name=f"mt5-bridge-{name}")
        for name, interval, body in specs
    )
    return BackgroundLoops(tasks=tasks)
