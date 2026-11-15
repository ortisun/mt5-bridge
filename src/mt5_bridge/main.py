"""Composition root: reads the environment, wires every dependency, serves.

Run with ``python -m mt5_bridge.main``. Every setting comes from the
environment, the optional terminal credentials included
(ADR-079, ``mt5_bridge.secrets``). Nothing here hardcodes an account, a secret,
or a symbol list meant for production. ``MT5_BRIDGE_CLIENT_BACKEND``
selects ``fake`` (default; local development/demo only) or ``real``
(Windows-only, requires ``mt5.real_client``; attaches to the logged-in
terminal unless explicit credentials are set, see ``resolve_terminal_connection``).
"""

import logging
import os
import sys
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import time, timedelta
from decimal import Decimal

from mt5_bridge.api.app import create_app
from mt5_bridge.api.app_state import AppState, HealthState
from mt5_bridge.api.audit import AuditLog
from mt5_bridge.clock import Clock, SystemClock
from mt5_bridge.core import ocsf
from mt5_bridge.core.clock_drift import ClockDriftState
from mt5_bridge.core.daily_loss_baseline import DailyLossBaseline, TradingDayPolicy
from mt5_bridge.core.database import BridgeDatabase
from mt5_bridge.core.dead_man_switch import DeadManSwitch
from mt5_bridge.core.kill_switch import KillSwitch
from mt5_bridge.core.limits_guard import LimitsConfig, LimitsGuard
from mt5_bridge.core.limits_state_provider import AccountEquityTracker, compute_limits_state
from mt5_bridge.core.log_formatting import JsonLogFormatter
from mt5_bridge.core.log_redaction import collect_bridge_secrets
from mt5_bridge.core.metrics import register_state_gauges
from mt5_bridge.core.observability import Observability, configure_observability
from mt5_bridge.core.order_journal import OrderJournal
from mt5_bridge.core.order_service import OrderService
from mt5_bridge.core.pollers import ExecutionPoller
from mt5_bridge.core.reconciliation import reconcile_on_boot
from mt5_bridge.core.replace_operations import ReplaceOperationStore
from mt5_bridge.core.sequence_log import SequenceLog
from mt5_bridge.core.supervisor import verify_account_binding, verify_netting_mode
from mt5_bridge.core.terminal_watchdog import TerminalReconnect, TerminalWatchdogState
from mt5_bridge.core.worker import BridgeWorker
from mt5_bridge.mt5.client import Mt5Client
from mt5_bridge.mt5.fake_client import FakeMt5Client
from mt5_bridge.mt5.models import (
    AccountInfo,
    AccountTradeMode,
    FillingMode,
    Mt5Credentials,
    PositionAccounting,
    SessionWindow,
    SymbolInfo,
    TerminalAttachment,
    TimeInForce,
)
from mt5_bridge.mt5.terminal_time import ServerWallClock, ServerWallClockTerminal
from mt5_bridge.secrets import EnvironmentSecretReader, SecretReader, terminal_credentials
from mt5_bridge.settings import BridgeSettings

logger = logging.getLogger("mt5_bridge")


@dataclass(frozen=True, slots=True)
class BootResult:
    state: AppState
    reconciled_resolved: tuple[str, ...]
    reconciled_unknown: tuple[str, ...]
    log_secrets: frozenset[str]
    """Every literal secret value this boot resolved — the raw material for
    ``core.log_redaction.install`` (plan §84); collected here, where every
    value is already in scope, rather than re-resolved in ``main()``."""


def _demo_symbols() -> tuple[SymbolInfo, ...]:
    """Seed data for the ``fake`` backend only — never used once
    ``MT5_BRIDGE_CLIENT_BACKEND=real``, where every rule comes from the
    terminal instead (plan §114: filling mode/validity read per symbol)."""
    sessions = tuple(
        SessionWindow(weekday=day, open_time=time(10, 0), close_time=time(17, 0))
        for day in range(5)
    )
    return (
        SymbolInfo(
            name="WINZ25",
            description="Mini index futures (local demo data)",
            currency_base="BRL",
            currency_profit="BRL",
            currency_margin="BRL",
            digits=0,
            tick_size=Decimal(5),
            tick_value=Decimal("1.00"),
            volume_min=Decimal(1),
            volume_max=Decimal(20),
            volume_step=Decimal(1),
            contract_size=Decimal(1),
            filling_modes=frozenset({FillingMode.RETURN}),
            time_in_force_modes=frozenset({TimeInForce.DAY}),
            is_continuous_series=False,
            trading_allowed=True,
            sessions=sessions,
        ),
    )


def _demo_account() -> AccountInfo:
    return AccountInfo(
        login=0,
        balance=Decimal(0),
        equity=Decimal(0),
        margin=Decimal(0),
        margin_free=Decimal(0),
        currency="BRL",
        position_accounting=PositionAccounting.NETTING,
        trade_allowed=False,
        server="demo",
        trade_mode=AccountTradeMode.DEMO,
    )


def resolve_terminal_connection(
    env: Mapping[str, str], secrets: SecretReader
) -> Mt5Credentials | TerminalAttachment:
    """Explicit login when the terminal secret is set, otherwise attach.

    Attaching to the terminal already logged in (plan §114: credentials stay
    in the VM terminal) requires ``MT5_BRIDGE_EXPECTED_LOGIN`` and
    ``MT5_BRIDGE_EXPECTED_SERVER`` so boot can prove which account it
    attached to; a partial credential secret is refused.
    """
    path = env.get("MT5_BRIDGE_TERMINAL_PATH")
    credentials = terminal_credentials(secrets)
    if credentials is not None:
        login, password, server = (credentials.get(k) for k in ("login", "password", "server"))
        if not (login and password and server):
            raise RuntimeError("the terminal secret must define login, password and server")
        return Mt5Credentials(login=int(login), password=password, server=server, path=path)
    for name in ("MT5_BRIDGE_EXPECTED_LOGIN", "MT5_BRIDGE_EXPECTED_SERVER"):
        if not env.get(name):
            raise RuntimeError(f"attaching to the logged-in terminal requires {name}")
    return TerminalAttachment(path=path)


def _resolve_terminal_password(env: Mapping[str, str], reader: SecretReader) -> str | None:
    """The terminal password, only when this boot would actually use one —
    the ``fake`` backend and terminal-attach mode never hold a real one, and
    ``resolve_terminal_connection`` already validated the ``real`` backend's
    credential shape (or refused boot) by the time this runs."""
    if env.get("MT5_BRIDGE_CLIENT_BACKEND", "fake") != "real":
        return None
    credentials = terminal_credentials(reader)
    return credentials.get("password") if credentials else None


def build_mt5_client(*, env: Mapping[str, str], clock: Clock, secrets: SecretReader) -> Mt5Client:
    """Build and connect the terminal client; apply a declared server time zone.

    ``MT5_BRIDGE_TERMINAL_TIME_ZONE`` names the IANA zone whose wall clock the
    terminal reports as epochs (a proven deviation, ``mt5.terminal_time``);
    unset keeps the documented UTC contract.
    """
    client = _build_terminal_client(env=env, clock=clock, secrets=secrets)
    zone = env.get("MT5_BRIDGE_TERMINAL_TIME_ZONE")
    return ServerWallClockTerminal(client, ServerWallClock(zone)) if zone else client


def terminal_reconnect_for(env: Mapping[str, str], secrets: SecretReader) -> TerminalReconnect:
    """The same ``connect``/``attach`` procedure ``_build_terminal_client``
    used at boot, re-runnable against the *existing* client instance by the
    watchdog (plan §114 UTM VM Operations) -- never a fresh
    ``RealMt5Client()``/``FakeMt5Client()``, only re-establishing the session
    on the one the worker thread already holds."""
    backend = env.get("MT5_BRIDGE_CLIENT_BACKEND", "fake")
    if backend != "real":
        demo_credentials = Mt5Credentials(login=0, password="", server="demo")
        return lambda client: client.connect(demo_credentials)
    connection = resolve_terminal_connection(env, secrets)
    if isinstance(connection, Mt5Credentials):
        return lambda client: client.connect(connection)
    return lambda client: client.attach(connection)


def _build_terminal_client(
    *, env: Mapping[str, str], clock: Clock, secrets: SecretReader
) -> Mt5Client:
    """Build and connect the terminal client per ``MT5_BRIDGE_CLIENT_BACKEND``."""
    backend = env.get("MT5_BRIDGE_CLIENT_BACKEND", "fake")
    if backend == "real":
        if sys.platform != "win32":
            raise RuntimeError(
                "MT5_BRIDGE_CLIENT_BACKEND=real requires Windows (sys.platform == 'win32')"
            )
        from mt5_bridge.mt5.real_client import RealMt5Client  # noqa: PLC0415 - Windows-only import

        client: Mt5Client = RealMt5Client()
        connection = resolve_terminal_connection(env, secrets)
        if isinstance(connection, Mt5Credentials):
            client.connect(connection)
        else:
            client.attach(connection)
        return client
    if backend == "fake":
        logger.warning(
            "MT5_BRIDGE_CLIENT_BACKEND=fake: local development/demo data only, never production"
        )
        fake = FakeMt5Client(symbols=_demo_symbols(), account=_demo_account(), clock=clock)
        fake.connect(Mt5Credentials(login=0, password="", server="demo"))
        return fake
    raise ValueError(f"unknown MT5_BRIDGE_CLIENT_BACKEND={backend!r}")


@dataclass(frozen=True, slots=True)
class ServeOptions:
    host: str
    port: int
    certfile: str | None
    keyfile: str | None


def serve_options(env: Mapping[str, str]) -> ServeOptions:
    """Plain HTTP by default; TLS is optional for any host, never mandatory
    (2026-09-29 user decision) -- setting both `MT5_BRIDGE_TLS_CERTFILE` and
    `MT5_BRIDGE_TLS_KEYFILE` serves TLS regardless of `MT5_BRIDGE_HOST`.

    In the VM the bridge may bind the VM address with a certificate the host
    pins (plan §114) or plain HTTP, an operator choice; switching a running
    deployment requires redeploying the bridge (docs/runbook.md).
    """
    host = env.get("MT5_BRIDGE_HOST", "127.0.0.1")
    port = int(env.get("MT5_BRIDGE_PORT", "8765"))
    certfile = env.get("MT5_BRIDGE_TLS_CERTFILE") or None
    keyfile = env.get("MT5_BRIDGE_TLS_KEYFILE") or None
    if (certfile is None) != (keyfile is None):
        raise RuntimeError("MT5_BRIDGE_TLS_CERTFILE and MT5_BRIDGE_TLS_KEYFILE must be set both")
    return ServeOptions(host=host, port=port, certfile=certfile, keyfile=keyfile)


def _load_limits_config(env: Mapping[str, str]) -> LimitsConfig:
    allowlist_raw = env.get("MT5_BRIDGE_SYMBOL_ALLOWLIST")
    allowlist = (
        frozenset(s.strip() for s in allowlist_raw.split(",") if s.strip())
        if allowlist_raw
        else None
    )
    return LimitsConfig(
        symbol_allowlist=allowlist,
        max_volume_per_order=Decimal(env.get("MT5_BRIDGE_MAX_VOLUME_PER_ORDER", "10")),
        max_orders_per_minute=int(env.get("MT5_BRIDGE_MAX_ORDERS_PER_MINUTE", "20")),
        max_open_notional=Decimal(env.get("MT5_BRIDGE_MAX_OPEN_NOTIONAL", "1000000")),
        max_daily_loss=Decimal(env.get("MT5_BRIDGE_MAX_DAILY_LOSS", "50000")),
    )


def boot(
    env: Mapping[str, str],
    secrets: SecretReader | None = None,
    observability: Observability | None = None,
) -> BootResult:
    """Build every dependency, run boot checks, and return a ready ``AppState``.

    Split from ``main()`` so tests exercise the exact composition path
    without needing a real terminal or an actual HTTP server (tests inject an
    ``InMemorySecretReader``; production reads the secret variables).
    ``main()`` passes the ``Observability`` it already installed process-wide;
    without one, boot builds its own, registered nowhere.
    """
    settings = BridgeSettings.from_env(env)
    trading_day_policy = TradingDayPolicy(settings.trading_day_time_zone)
    reader = secrets if secrets is not None else EnvironmentSecretReader(env)
    clock: Clock = SystemClock()
    client = build_mt5_client(env=env, clock=clock, secrets=reader)

    verify_netting_mode(client, expected=settings.expected_position_accounting)
    verify_account_binding(
        client,
        environment=settings.environment,
        expected_login=settings.expected_login,
        expected_server=settings.expected_server,
    )
    account = client.account_info()

    # Telemetry (plan §84, ADR-095): available before anything that records
    # a metric (KillSwitch, OrderService) is constructed. The metrics
    # registry backs GET /v1/metrics (api.routes_metrics) -- no separate
    # listener.
    observability = observability or configure_observability(env)
    metrics = observability.metrics

    db = BridgeDatabase(settings.db_path)
    journal = OrderJournal(db)
    sequence_log = SequenceLog(db)
    kill_switch = KillSwitch(db, now=clock.now_utc(), metrics=metrics)
    dead_man_switch = DeadManSwitch(timeout_seconds=settings.dead_man_timeout_seconds)
    worker = BridgeWorker(client, max_market_poll_backlog=settings.market_poll_backlog_limit)
    worker.start()

    report = reconcile_on_boot(
        journal=journal,
        sequence_log=sequence_log,
        db=db,
        client=client,
        clock=clock,
        window=timedelta(hours=settings.reconciliation_window_hours),
    )
    if report.resolved or report.unknown:
        logger.info("boot reconciliation: resolved=%s unknown=%s", report.resolved, report.unknown)

    clock_drift_state = ClockDriftState()
    equity_tracker = AccountEquityTracker()
    boot_time = clock.now_utc()
    baseline = DailyLossBaseline(db).resolve(
        account_id=str(account.login),
        trading_day_policy=trading_day_policy,
        now=boot_time,
        current_equity=account.equity,
    )
    if baseline.stale:
        # Never silently reset: a foreign baseline is neither trusted nor
        # overwritten (audit F1, plan rule 43). LimitsGuard fails closed on
        # every submission until an operator resolves the mismatch.
        logger.error("daily-loss baseline unavailable: %s", baseline.reason)
        equity_tracker.mark_unavailable(baseline.reason or "daily-loss baseline unavailable")
    else:
        equity_tracker.record(baseline.baseline_equity, now=boot_time)
    equity_tracker.record(account.equity, now=boot_time)  # current reading either way

    terminal_watchdog_state = TerminalWatchdogState(connected=True)
    terminal_reconnect = terminal_reconnect_for(env, reader)
    order_service = OrderService(
        db=db,
        journal=journal,
        replace_operations=ReplaceOperationStore(db),
        sequence_log=sequence_log,
        limits_guard=LimitsGuard(_load_limits_config(env)),
        kill_switch=kill_switch,
        worker=worker,
        clock=clock,
        magic=settings.magic,
        limits_state_provider=lambda: compute_limits_state(
            journal=journal,
            equity_tracker=equity_tracker,
            clock=clock,
            equity_max_age_seconds=settings.equity_max_age_seconds,
        ),
        clock_drift_state=clock_drift_state,
        clock_drift_threshold_seconds=settings.clock_drift_threshold_seconds,
        metrics=metrics,
        reconciliation_window=timedelta(hours=settings.reconciliation_window_hours),
        quote_max_age_seconds=settings.quote_max_age_seconds,
        terminal_watchdog_state=terminal_watchdog_state,
    )
    log_secrets = collect_bridge_secrets(terminal_password=_resolve_terminal_password(env, reader))
    execution_poller = ExecutionPoller(
        db=db,
        journal=journal,
        sequence_log=sequence_log,
        overlap_seconds=settings.execution_poll_overlap_seconds,
    )

    health = HealthState()
    register_state_gauges(
        observability.meter,
        kill_switch=kill_switch,
        dead_man_switch=dead_man_switch,
        terminal_watchdog_state=terminal_watchdog_state,
        clock_drift_state=clock_drift_state,
        health=health,
        clock=clock,
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
        execution_poller=execution_poller,
        clock_drift_state=clock_drift_state,
        equity_tracker=equity_tracker,
        expected_account_id=str(account.login),
        trading_day_policy=trading_day_policy,
        terminal_watchdog_state=terminal_watchdog_state,
        auction_participation_supported=settings.auction_participation_supported,
        auction_window=timedelta(seconds=settings.auction_window_seconds),
        terminal_reconnect=terminal_reconnect,
        health=health,
        metrics_registry=observability.registry,
        metrics=metrics,
        meter=observability.meter,
    )
    return BootResult(
        state=state,
        reconciled_resolved=report.resolved,
        reconciled_unknown=report.unknown,
        log_secrets=log_secrets,
    )


def main() -> None:  # pragma: no cover - exercised via boot()/create_app() in tests
    import uvicorn

    from mt5_bridge.core.log_redaction import install as install_log_redaction

    logging.basicConfig(level=logging.INFO)
    # One JSON object per record (plan §3.13, §84); the redacting filter
    # installed below still runs first, since it attaches to these same
    # handlers rather than replacing them.
    formatter = JsonLogFormatter(deployment_environment=os.environ.get("MT5_BRIDGE_ENVIRONMENT"))
    for handler in logging.getLogger().handlers:
        handler.setFormatter(formatter)
    # Traces, metrics and logs (docs/observability.md): one bootstrap, whose
    # shutdown flushes all three at exit. Its log handler joins root here, so
    # the redaction installed below covers it like the stdout handler.
    observability = configure_observability(os.environ)
    observability.install()
    options = serve_options(os.environ)
    result = boot(os.environ, observability=observability)
    install_log_redaction(secrets=result.log_secrets)
    observability.redact(result.log_secrets)
    app = create_app(result.state, start_background_loops_on_startup=True)
    logger.info("mt5-bridge starting", extra=ocsf.lifecycle_log_extra(ocsf.LifecycleActivity.START))
    uvicorn.run(
        app,
        host=options.host,
        port=options.port,
        ssl_certfile=options.certfile,
        ssl_keyfile=options.keyfile,
        # uvicorn's own logging config would attach its own, unfiltered
        # handlers and stop its records propagating to root — disabled so
        # every uvicorn record reaches the redacting handler installed above.
        log_config=None,
    )
    logger.info("mt5-bridge stopped", extra=ocsf.lifecycle_log_extra(ocsf.LifecycleActivity.STOP))


if __name__ == "__main__":  # pragma: no cover
    main()
