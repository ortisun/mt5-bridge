"""Everything a route or the WS endpoint needs, bundled behind ``request.app.state``.

One dataclass instead of scattering globals: routes call ``get_state(request)``
and never reach into ``request.app.state`` directly, so the dependency is
typed and greppable.
"""

from dataclasses import dataclass, field
from datetime import datetime, timedelta

from fastapi import Request
from prometheus_client import CollectorRegistry

from mt5_bridge.api.audit import AuditLog
from mt5_bridge.clock import Clock
from mt5_bridge.core.clock_drift import ClockDriftState
from mt5_bridge.core.daily_loss_baseline import TradingDayPolicy
from mt5_bridge.core.dead_man_switch import DeadManSwitch
from mt5_bridge.core.kill_switch import KillSwitch
from mt5_bridge.core.limits_state_provider import AccountEquityTracker
from mt5_bridge.core.metrics_port import Metrics, NullMetrics
from mt5_bridge.core.order_journal import OrderJournal
from mt5_bridge.core.order_service import OrderService
from mt5_bridge.core.pollers import ExecutionPoller
from mt5_bridge.core.sequence_log import SequenceLog
from mt5_bridge.core.terminal_watchdog import TerminalReconnect, TerminalWatchdogState
from mt5_bridge.core.worker import BridgeWorker
from mt5_bridge.settings import BridgeSettings


def _noop_reconnect(client: object) -> None:
    """Safe placeholder for tests that never exercise the watchdog loop."""
    del client


@dataclass
class HealthState:
    """Mutable, updated by the background loops wired in ``main.py``.

    ``clock_drift_seconds`` deliberately is not duplicated here: it lives on
    the single shared ``AppState.clock_drift_state`` that ``OrderService``
    also reads, so the two can never disagree.
    """

    last_execution_poll_at: datetime | None = None
    tick_poll_latency_seconds: float | None = None
    bar_poll_latency_seconds: float | None = None


@dataclass(frozen=True, slots=True)
class AppState:
    settings: BridgeSettings
    worker: BridgeWorker
    order_service: OrderService
    journal: OrderJournal
    sequence_log: SequenceLog
    kill_switch: KillSwitch
    dead_man_switch: DeadManSwitch
    audit: AuditLog
    clock: Clock
    execution_poller: ExecutionPoller
    clock_drift_state: ClockDriftState
    equity_tracker: AccountEquityTracker
    expected_account_id: str
    trading_day_policy: TradingDayPolicy
    terminal_watchdog_state: TerminalWatchdogState
    auction_participation_supported: bool
    auction_window: timedelta
    health: HealthState = field(default_factory=HealthState)
    terminal_reconnect: TerminalReconnect = _noop_reconnect
    # Read by api.routes_metrics (GET /v1/metrics); populated by
    # core.metrics.register_state_gauges in main.boot. Defaults to an empty
    # registry so every existing test app stays unaffected.
    metrics_registry: CollectorRegistry = field(default_factory=CollectorRegistry)
    # The Metrics Port every event-recording call site reads (plan §84);
    # defaults to the explicit no-telemetry choice so every existing test
    # app stays unaffected.
    metrics: Metrics = field(default_factory=NullMetrics)


def get_state(request: Request) -> AppState:
    state = request.app.state.bridge
    if not isinstance(state, AppState):  # pragma: no cover - programming error guard
        raise RuntimeError("app.state.bridge was not initialized with AppState")
    return state
