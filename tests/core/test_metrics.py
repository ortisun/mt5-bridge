"""register_state_gauges: kill-switch/dead-man/terminal/clock-drift/poll gauges (plan §84)."""

from datetime import UTC, datetime
from typing import cast

from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import InMemoryMetricReader, NumberDataPoint

from mt5_bridge.api.app_state import HealthState
from mt5_bridge.core.clock_drift import ClockDriftState
from mt5_bridge.core.database import BridgeDatabase
from mt5_bridge.core.dead_man_switch import DeadManSwitch
from mt5_bridge.core.kill_switch import KillSwitch, KillSwitchAction
from mt5_bridge.core.metrics import register_state_gauges
from mt5_bridge.core.metrics_port import NullMetrics
from mt5_bridge.core.terminal_watchdog import TerminalWatchdogState

from testkit.clock import FixedClock

NOW = datetime(2026, 1, 1, tzinfo=UTC)


def _metric_points(reader: InMemoryMetricReader) -> dict[str, list[NumberDataPoint]]:
    data = reader.get_metrics_data()
    assert data is not None
    points: dict[str, list[NumberDataPoint]] = {}
    for resource_metrics in data.resource_metrics:
        for scope_metrics in resource_metrics.scope_metrics:
            for metric in scope_metrics.metrics:
                points[metric.name] = cast("list[NumberDataPoint]", list(metric.data.data_points))
    return points


def test_gauges_report_normal_state_by_default() -> None:
    clock = FixedClock(int(NOW.timestamp() * 1000))
    reader = InMemoryMetricReader()
    provider = MeterProvider(metric_readers=[reader])
    register_state_gauges(
        provider.get_meter("test"),
        kill_switch=KillSwitch(BridgeDatabase(":memory:"), now=NOW, metrics=NullMetrics()),
        dead_man_switch=DeadManSwitch(timeout_seconds=30),
        terminal_watchdog_state=TerminalWatchdogState(connected=True),
        clock_drift_state=ClockDriftState(),
        health=HealthState(),
        clock=clock,
    )

    points = _metric_points(reader)

    assert points["mt5_bridge.kill_switch.state"][0].value == 0
    assert points["mt5_bridge.dead_man_switch.armed"][0].value == 0
    assert points["mt5_bridge.terminal.connected"][0].value == 1
    assert points["mt5_bridge.clock_drift.seconds"][0].value == 0.0
    assert (
        "mt5_bridge.poll.tick_latency" not in points or points["mt5_bridge.poll.tick_latency"] == []
    )


def test_gauges_reflect_a_halted_switch_and_disconnected_terminal() -> None:
    clock = FixedClock(int(NOW.timestamp() * 1000))
    kill_switch = KillSwitch(BridgeDatabase(":memory:"), now=NOW, metrics=NullMetrics())
    kill_switch.activate(KillSwitchAction.HALT_NEW_ORDERS, reason="test", now=NOW)
    reader = InMemoryMetricReader()
    provider = MeterProvider(metric_readers=[reader])
    register_state_gauges(
        provider.get_meter("test"),
        kill_switch=kill_switch,
        dead_man_switch=DeadManSwitch(timeout_seconds=30),
        terminal_watchdog_state=TerminalWatchdogState(connected=False),
        clock_drift_state=ClockDriftState(drift_seconds=1.5),
        health=HealthState(
            tick_poll_latency_seconds=0.01,
            bar_poll_latency_seconds=0.02,
            last_execution_poll_at=NOW,
        ),
        clock=clock,
    )

    points = _metric_points(reader)

    assert points["mt5_bridge.kill_switch.state"][0].value == 1
    assert points["mt5_bridge.terminal.connected"][0].value == 0
    assert points["mt5_bridge.clock_drift.seconds"][0].value == 1.5
    assert points["mt5_bridge.poll.tick_latency"][0].value == 0.01
    assert points["mt5_bridge.poll.bar_latency"][0].value == 0.02
    assert points["mt5_bridge.poll.execution_latency"][0].value == 0.0


def test_a_heartbeat_arms_the_dead_man_switch_and_expiry_follows_the_clock() -> None:
    clock = FixedClock(int(NOW.timestamp() * 1000))
    switch = DeadManSwitch(timeout_seconds=1)
    switch.heartbeat(now_ms=clock.now_ms())
    reader = InMemoryMetricReader()
    provider = MeterProvider(metric_readers=[reader])
    register_state_gauges(
        provider.get_meter("test"),
        kill_switch=KillSwitch(BridgeDatabase(":memory:"), now=NOW, metrics=NullMetrics()),
        dead_man_switch=switch,
        terminal_watchdog_state=TerminalWatchdogState(connected=True),
        clock_drift_state=ClockDriftState(),
        health=HealthState(),
        clock=clock,
    )

    points = _metric_points(reader)
    assert points["mt5_bridge.dead_man_switch.armed"][0].value == 1
    assert points["mt5_bridge.dead_man_switch.expired"][0].value == 0

    clock.advance(2)
    points = _metric_points(reader)
    assert points["mt5_bridge.dead_man_switch.expired"][0].value == 1
