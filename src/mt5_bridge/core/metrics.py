"""Prometheus-compatible metrics for the bridge process (plan §84).

``configure_metrics`` builds one process-local ``MeterProvider`` with an
always-attached Prometheus pull reader (cheap; costs nothing unless
scraped -- ``api.routes_metrics`` exposes it on the existing FastAPI
app). ``core.observability`` calls it with the shared
``Resource`` and, when OTLP metric export is configured, one more reader;
the provider is never registered as the process global here.
``register_state_gauges`` registers the
observable gauges this delivery wires: kill-switch state, dead-man-switch
armed/expired, terminal connectivity, clock drift, and tick/bar/execution
poll latency -- each read straight from in-memory state a background loop
already maintains (``core.pollers``, ``core.terminal_watchdog``,
``core.clock_drift``), so a scrape never performs I/O of its own. Accepts
only ``core``-owned types (never ``api.app_state.AppState``): ``core`` does
not depend on ``api`` anywhere else in this codebase, and this module keeps
that direction.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

from opentelemetry.exporter.prometheus import PrometheusMetricReader
from opentelemetry.metrics import CallbackOptions, Meter, Observation
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import MetricReader
from opentelemetry.sdk.resources import Resource
from prometheus_client import CollectorRegistry

from mt5_bridge.clock import Clock
from mt5_bridge.core.clock_drift import ClockDriftState
from mt5_bridge.core.dead_man_switch import DeadManSwitch
from mt5_bridge.core.kill_switch import KillSwitch, KillSwitchState
from mt5_bridge.core.log_formatting import SERVICE_NAME
from mt5_bridge.core.metrics_port import Metrics, OtelMetrics
from mt5_bridge.core.terminal_watchdog import TerminalWatchdogState

_KILL_SWITCH_SEVERITY: dict[KillSwitchState, int] = {
    KillSwitchState.NORMAL: 0,
    KillSwitchState.HALTED: 1,
    KillSwitchState.CANCELED_ALL: 2,
    KillSwitchState.FLATTENED: 3,
}


class PollHealth(Protocol):
    """The subset of ``api.app_state.HealthState`` these gauges read."""

    tick_poll_latency_seconds: float | None
    bar_poll_latency_seconds: float | None
    last_execution_poll_at: datetime | None


@dataclass(frozen=True, slots=True)
class MetricsBootstrap:
    provider: MeterProvider
    meter: Meter
    registry: CollectorRegistry
    metrics: Metrics


def configure_metrics(
    env: Mapping[str, str],
    *,
    resource: Resource | None = None,
    extra_readers: Sequence[MetricReader] = (),
) -> MetricsBootstrap:
    """The Prometheus pull reader is unconditional; ``extra_readers`` adds a
    push reader chosen by ``core.observability`` (``env`` selects nothing here)."""
    del env
    registry = CollectorRegistry()
    provider = MeterProvider(
        resource=resource or Resource.create({"service.name": SERVICE_NAME}),
        metric_readers=[PrometheusMetricReader(registry=registry), *extra_readers],
    )
    meter = provider.get_meter(SERVICE_NAME)
    return MetricsBootstrap(
        provider=provider, meter=meter, registry=registry, metrics=OtelMetrics(meter)
    )


def register_state_gauges(
    meter: Meter,
    *,
    kill_switch: KillSwitch,
    dead_man_switch: DeadManSwitch,
    terminal_watchdog_state: TerminalWatchdogState,
    clock_drift_state: ClockDriftState,
    health: PollHealth,
    clock: Clock,
) -> None:
    def kill_switch_severity(options: CallbackOptions) -> Iterable[Observation]:
        del options
        status = kill_switch.status()
        yield Observation(_KILL_SWITCH_SEVERITY[status.state], {"state": status.state.value})

    def dead_man_armed(options: CallbackOptions) -> Iterable[Observation]:
        del options
        yield Observation(1 if dead_man_switch.armed else 0)

    def dead_man_expired(options: CallbackOptions) -> Iterable[Observation]:
        del options
        now_ms = round(clock.now_utc().timestamp() * 1000)
        yield Observation(1 if dead_man_switch.is_expired(now_ms) else 0)

    def terminal_connected(options: CallbackOptions) -> Iterable[Observation]:
        del options
        yield Observation(1 if terminal_watchdog_state.connected else 0)

    def clock_drift_seconds(options: CallbackOptions) -> Iterable[Observation]:
        del options
        yield Observation(clock_drift_state.drift_seconds)

    def tick_poll_latency(options: CallbackOptions) -> Iterable[Observation]:
        del options
        if health.tick_poll_latency_seconds is not None:
            yield Observation(health.tick_poll_latency_seconds)

    def bar_poll_latency(options: CallbackOptions) -> Iterable[Observation]:
        del options
        if health.bar_poll_latency_seconds is not None:
            yield Observation(health.bar_poll_latency_seconds)

    def execution_poll_latency(options: CallbackOptions) -> Iterable[Observation]:
        del options
        last_poll = health.last_execution_poll_at
        if last_poll is not None:
            yield Observation((clock.now_utc() - last_poll).total_seconds())

    meter.create_observable_gauge(
        "mt5_bridge.kill_switch.state",
        callbacks=[kill_switch_severity],
        unit="1",
        description="Kill-switch severity: 0 NORMAL, 1 HALTED, 2 CANCELED_ALL, 3 FLATTENED",
    )
    meter.create_observable_gauge(
        "mt5_bridge.dead_man_switch.armed",
        callbacks=[dead_man_armed],
        unit="1",
        description="1 while a heartbeat lease is established",
    )
    meter.create_observable_gauge(
        "mt5_bridge.dead_man_switch.expired",
        callbacks=[dead_man_expired],
        unit="1",
        description="1 when the established lease's deadline has passed",
    )
    meter.create_observable_gauge(
        "mt5_bridge.terminal.connected",
        callbacks=[terminal_connected],
        unit="1",
        description="1 while the watchdog considers the MT5 terminal connected",
    )
    meter.create_observable_gauge(
        "mt5_bridge.clock_drift.seconds",
        callbacks=[clock_drift_seconds],
        unit="s",
        description="Last measured drift between the terminal clock and NTP",
    )
    meter.create_observable_gauge(
        "mt5_bridge.poll.tick_latency",
        callbacks=[tick_poll_latency],
        unit="s",
        description="Duration of the most recent tick poll",
    )
    meter.create_observable_gauge(
        "mt5_bridge.poll.bar_latency",
        callbacks=[bar_poll_latency],
        unit="s",
        description="Duration of the most recent bar poll",
    )
    meter.create_observable_gauge(
        "mt5_bridge.poll.execution_latency",
        callbacks=[execution_poll_latency],
        unit="s",
        description="Age of the most recent execution poll",
    )


__all__ = ["MetricsBootstrap", "PollHealth", "configure_metrics", "register_state_gauges"]
