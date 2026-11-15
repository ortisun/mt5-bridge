"""Shared app-building helper for API-layer tests."""

from collections.abc import Callable
from datetime import timedelta
from decimal import Decimal

from fastapi import FastAPI

from mt5_bridge.api.app import create_app
from mt5_bridge.api.app_state import AppState, HealthState
from mt5_bridge.api.audit import AuditLog
from mt5_bridge.core.clock_drift import ClockDriftState
from mt5_bridge.core.daily_loss_baseline import TradingDayPolicy
from mt5_bridge.core.database import BridgeDatabase
from mt5_bridge.core.dead_man_switch import DeadManSwitch
from mt5_bridge.core.kill_switch import KillSwitch
from mt5_bridge.core.limits_guard import LimitsConfig, LimitsGuard
from mt5_bridge.core.limits_state_provider import AccountEquityTracker, compute_limits_state
from mt5_bridge.core.metrics import configure_metrics, register_state_gauges
from mt5_bridge.core.order_journal import OrderJournal
from mt5_bridge.core.order_service import OrderService
from mt5_bridge.core.pollers import ExecutionPoller
from mt5_bridge.core.replace_operations import ReplaceOperationStore
from mt5_bridge.core.sequence_log import SequenceLog
from mt5_bridge.core.terminal_watchdog import TerminalWatchdogState
from mt5_bridge.core.worker import BridgeWorker
from mt5_bridge.mt5.client import Mt5Client
from mt5_bridge.mt5.fake_client import FakeMt5Client
from mt5_bridge.mt5.models import Mt5Credentials
from mt5_bridge.settings import BridgeSettings

from testkit.clock import FixedClock
from testkit.fixtures import default_account, default_symbols

NOW_MS = 1_700_000_000_000


def permissive_limits_config() -> LimitsConfig:
    return LimitsConfig(
        symbol_allowlist=None,
        max_volume_per_order=Decimal(1000),
        max_orders_per_minute=1000,
        max_open_notional=Decimal(10_000_000),
        max_daily_loss=Decimal(10_000_000),
    )


def build_test_app_state(
    *,
    clock: FixedClock | None = None,
    limits_config: LimitsConfig | None = None,
    dead_man_timeout_seconds: float = 30,
    max_market_poll_backlog: int = 1000,
    terminal_wrapper: Callable[[FakeMt5Client], Mt5Client] | None = None,
    auction_participation_supported: bool = False,
    auction_window_seconds: float = 300,
    terminal_watchdog_state: TerminalWatchdogState | None = None,
) -> tuple[AppState, FakeMt5Client]:
    """``terminal_wrapper`` lets a test wire a decorator (e.g.
    ``ServerWallClockTerminal``) between the worker and the fake terminal —
    the returned ``FakeMt5Client`` is still the one to call ``set_tick``/
    ``push_bar`` on; the worker itself reads through the wrapper."""
    clock = clock or FixedClock(NOW_MS)
    db = BridgeDatabase(":memory:")
    account = default_account()
    client = FakeMt5Client(symbols=default_symbols(), account=account, clock=clock)
    client.connect(Mt5Credentials(login=1, password="x", server="Demo"))
    worker_terminal: Mt5Client = client if terminal_wrapper is None else terminal_wrapper(client)
    worker = BridgeWorker(worker_terminal, max_market_poll_backlog=max_market_poll_backlog)
    worker.start()

    journal = OrderJournal(db)
    sequence_log = SequenceLog(db)
    metrics_bootstrap = configure_metrics({})
    metrics = metrics_bootstrap.metrics
    kill_switch = KillSwitch(db, now=clock.now_utc(), metrics=metrics)
    dead_man_switch = DeadManSwitch(timeout_seconds=dead_man_timeout_seconds)
    limits_guard = LimitsGuard(limits_config or permissive_limits_config())
    equity_tracker = AccountEquityTracker()
    equity_tracker.record(account.equity)
    clock_drift_state = ClockDriftState()
    watchdog_state = terminal_watchdog_state or TerminalWatchdogState()
    health = HealthState()
    register_state_gauges(
        metrics_bootstrap.meter,
        kill_switch=kill_switch,
        dead_man_switch=dead_man_switch,
        terminal_watchdog_state=watchdog_state,
        clock_drift_state=clock_drift_state,
        health=health,
        clock=clock,
    )
    order_service = OrderService(
        db=db,
        journal=journal,
        replace_operations=ReplaceOperationStore(db),
        sequence_log=sequence_log,
        limits_guard=limits_guard,
        kill_switch=kill_switch,
        worker=worker,
        clock=clock,
        magic=555,
        limits_state_provider=lambda: compute_limits_state(
            journal=journal, equity_tracker=equity_tracker, clock=clock
        ),
        clock_drift_state=clock_drift_state,
        clock_drift_threshold_seconds=2.0,
        metrics=metrics,
        terminal_watchdog_state=watchdog_state,
    )
    settings = BridgeSettings.from_env(
        {
            "MT5_BRIDGE_AUCTION_PARTICIPATION_SUPPORTED": (
                "true" if auction_participation_supported else "false"
            ),
            "MT5_BRIDGE_AUCTION_WINDOW_SECONDS": str(auction_window_seconds),
        }
    )
    state = AppState(
        settings=settings,
        worker=worker,
        order_service=order_service,
        journal=journal,
        sequence_log=sequence_log,
        kill_switch=kill_switch,
        dead_man_switch=dead_man_switch,
        audit=AuditLog(db),
        clock=clock,
        execution_poller=ExecutionPoller(db=db, journal=journal, sequence_log=sequence_log),
        clock_drift_state=clock_drift_state,
        equity_tracker=equity_tracker,
        expected_account_id=str(account.login),
        trading_day_policy=TradingDayPolicy(settings.trading_day_time_zone),
        terminal_watchdog_state=watchdog_state,
        auction_participation_supported=settings.auction_participation_supported,
        auction_window=timedelta(seconds=settings.auction_window_seconds),
        health=health,
        metrics_registry=metrics_bootstrap.registry,
        metrics=metrics,
        meter=metrics_bootstrap.meter,
    )
    return state, client


def build_test_app(
    *,
    clock: FixedClock | None = None,
    limits_config: LimitsConfig | None = None,
    dead_man_timeout_seconds: float = 30,
    max_market_poll_backlog: int = 1000,
    terminal_wrapper: Callable[[FakeMt5Client], Mt5Client] | None = None,
    auction_participation_supported: bool = False,
    auction_window_seconds: float = 300,
    terminal_watchdog_state: TerminalWatchdogState | None = None,
) -> tuple[FastAPI, FakeMt5Client, AppState]:
    state, client = build_test_app_state(
        clock=clock,
        limits_config=limits_config,
        dead_man_timeout_seconds=dead_man_timeout_seconds,
        max_market_poll_backlog=max_market_poll_backlog,
        terminal_wrapper=terminal_wrapper,
        auction_participation_supported=auction_participation_supported,
        auction_window_seconds=auction_window_seconds,
        terminal_watchdog_state=terminal_watchdog_state,
    )
    return create_app(state), client, state
