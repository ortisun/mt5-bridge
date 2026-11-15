"""End-to-end proof that the safety components are wired into the running
app, not just unit-tested in isolation (plan §107, §114): a real FastAPI app
(``create_app``), a real ASGI HTTP client, ``FakeMt5Client`` standing in for
the terminal, and an injected ``FixedClock``. Each background loop's body is
called directly (``asyncio.run(...)``) instead of waiting on a real timer —
deterministic, and exactly what the loop would do at its next tick.
"""

import asyncio
from decimal import Decimal

import pytest

from mt5_bridge.api.background import (
    measure_clock_drift_once,
    poll_executions_once,
    sweep_dead_man_once,
    watch_terminal_once,
)
from mt5_bridge.core.limits_guard import LimitsConfig
from mt5_bridge.core.terminal_watchdog import WatchdogBackoffPolicy

from testkit.app_builder import (
    NOW_MS,
    build_test_app,
    permissive_limits_config,
)
from testkit.clock import FixedClock
from testkit.fixtures import WINZ25
from testkit.http_client import TypedTestClient


def limit_order_body(
    client_order_id: str, *, price: str = "50", volume: str = "1"
) -> dict[str, object]:
    return {
        "client_order_id": client_order_id,
        "symbol": WINZ25,
        "side": "BUY",
        "type": "LIMIT",
        "price": price,
        "volume": volume,
        "time_in_force": "DAY",
    }


class TestExecutionPollerIsWired:
    def test_an_asynchronous_fill_only_appears_after_the_poller_runs(self) -> None:
        app, fake_client, state = build_test_app()
        with TypedTestClient(app) as http:
            submitted = http.post("/v1/orders", json=limit_order_body("co-poll-1"))
            assert submitted.json()["state"] == "ACKNOWLEDGED"
            ticket = submitted.json()["ticket"]

            fake_client.force_fill(ticket)  # a fill the bridge did not initiate itself

            still_stale = http.get("/v1/orders/co-poll-1")
            assert still_stale.json()["state"] == "ACKNOWLEDGED"

            asyncio.run(poll_executions_once(state))

            after_poll = http.get("/v1/orders/co-poll-1")
            assert after_poll.json()["state"] == "FILLED"

            health = http.get("/v1/health")
            assert health.json()["execution_poll_latency_seconds"] is not None


class TestKillSwitchCancelAllIsWired:
    def test_cancel_all_actually_cancels_every_open_order(self) -> None:
        app, _fake_client, _state = build_test_app()
        with TypedTestClient(app) as http:
            first = http.post(
                "/v1/orders",
                json=limit_order_body("co-ka-1", price="50"),
            )
            second = http.post(
                "/v1/orders",
                json=limit_order_body("co-ka-2", price="55"),
            )
            assert first.json()["state"] == "ACKNOWLEDGED"
            assert second.json()["state"] == "ACKNOWLEDGED"

            kill = http.post("/v1/kill-switch", json={"action": "CANCEL_ALL"})
            assert kill.status_code == 200
            assert kill.json()["state"] == "CANCELED_ALL"

            for client_order_id in ("co-ka-1", "co-ka-2"):
                entry = http.get(f"/v1/orders/{client_order_id}")
                assert entry.json()["state"] == "CANCELED"


class TestKillSwitchFlattenIsWired:
    def test_flatten_actually_submits_a_closing_market_order(self) -> None:
        app, fake_client, _state = build_test_app()
        fake_client.set_tick(
            WINZ25, bid=Decimal(100), ask=Decimal(101), last=Decimal(100), volume=Decimal(1)
        )
        with TypedTestClient(app) as http:
            opened = http.post(
                "/v1/orders",
                json={
                    "client_order_id": "co-fl-e2e-1",
                    "symbol": WINZ25,
                    "side": "BUY",
                    "type": "MARKET",
                    "volume": "2",
                    "time_in_force": "DAY",
                },
            )
            assert opened.json()["state"] == "FILLED"
            assert fake_client.positions()

            kill = http.post("/v1/kill-switch", json={"action": "FLATTEN"})

        assert kill.status_code == 200
        assert kill.json()["state"] == "FLATTENED"
        assert fake_client.positions() == ()  # actually closed, not merely promised


class TestDeadManSweepIsWired:
    def test_a_stalled_heartbeat_triggers_cancel_all(self) -> None:
        clock = FixedClock(NOW_MS)
        app, _fake_client, state = build_test_app(dead_man_timeout_seconds=1, clock=clock)
        with TypedTestClient(app) as http:
            http.post("/v1/heartbeat")
            submitted = http.post("/v1/orders", json=limit_order_body("co-dm-1"))
            assert submitted.json()["state"] == "ACKNOWLEDGED"

            clock.advance(5)  # well past the 1-second lease timeout, no further heartbeat

            expired = asyncio.run(sweep_dead_man_once(state))
            assert expired is True

            health = http.get("/v1/health")
            assert health.json()["kill_switch_state"] == "CANCELED_ALL"
            entry = http.get("/v1/orders/co-dm-1")
            assert entry.json()["state"] == "CANCELED"

    def test_an_expired_lease_fires_once_so_a_rearm_holds(self) -> None:
        """Seen on the demo terminal: an expired lease kept firing every sweep,
        re-engaging CANCEL_ALL right after any rearm and re-cancelling against
        the terminal each second. It fires once, then waits for a heartbeat."""
        clock = FixedClock(NOW_MS)
        app, _fake_client, state = build_test_app(dead_man_timeout_seconds=1, clock=clock)
        with TypedTestClient(app) as http:
            http.post("/v1/heartbeat")
            clock.advance(5)
            assert asyncio.run(sweep_dead_man_once(state)) is True

            http.delete("/v1/kill-switch")
            clock.advance(5)

            assert asyncio.run(sweep_dead_man_once(state)) is False
            health = http.get("/v1/health")
            assert health.json()["kill_switch_state"] == "NORMAL"

    def test_a_new_heartbeat_after_firing_arms_the_lease_again(self) -> None:
        clock = FixedClock(NOW_MS)
        app, _fake_client, state = build_test_app(dead_man_timeout_seconds=1, clock=clock)
        with TypedTestClient(app) as http:
            http.post("/v1/heartbeat")
            clock.advance(5)
            assert asyncio.run(sweep_dead_man_once(state)) is True
            http.delete("/v1/kill-switch")

            http.post("/v1/heartbeat")
            clock.advance(5)

            assert asyncio.run(sweep_dead_man_once(state)) is True

    def test_a_cancel_all_that_fails_keeps_the_lease_expired_for_the_next_sweep(self) -> None:
        clock = FixedClock(NOW_MS)
        app, fake_client, state = build_test_app(dead_man_timeout_seconds=1, clock=clock)
        with TypedTestClient(app) as http:
            http.post("/v1/heartbeat")
            http.post("/v1/orders", json=limit_order_body("co-dm-x"))
            clock.advance(5)
            fake_client.simulate_disconnect()

            with pytest.raises(Exception):  # noqa: B017 - any failure must leave it armed
                asyncio.run(sweep_dead_man_once(state))

            fake_client.simulate_reconnect()
            assert asyncio.run(sweep_dead_man_once(state)) is True
            entry = http.get("/v1/orders/co-dm-x")
            assert entry.json()["state"] == "CANCELED"

    def test_a_fresh_heartbeat_never_triggers_a_sweep(self) -> None:
        clock = FixedClock(NOW_MS)
        app, _fake_client, state = build_test_app(dead_man_timeout_seconds=30, clock=clock)
        with TypedTestClient(app) as http:
            http.post("/v1/heartbeat")

            clock.advance(1)

            assert asyncio.run(sweep_dead_man_once(state)) is False
            health = http.get("/v1/health")
            assert health.json()["kill_switch_state"] == "NORMAL"


_WATCHDOG_BACKOFF = WatchdogBackoffPolicy(base_seconds=1.0, max_seconds=8.0)


class TestTerminalWatchdogIsWired:
    def test_a_dropped_terminal_is_reported_and_blocks_new_orders(self) -> None:
        app, fake_client, state = build_test_app()
        with TypedTestClient(app) as http:
            baseline = http.get("/v1/health")
            assert baseline.json()["terminal_connected"] is True

            fake_client.simulate_disconnect()
            changed = asyncio.run(watch_terminal_once(state, backoff=_WATCHDOG_BACKOFF))
            assert changed is True

            after = http.get("/v1/health")
            assert after.json()["terminal_connected"] is False

            rejected = http.post("/v1/orders", json=limit_order_body("co-watchdog-1"))
            assert rejected.json()["state"] == "REJECTED"
            assert rejected.json()["reason"] == "terminal disconnected"

    def test_a_recovered_terminal_unblocks_new_orders_again(self) -> None:
        app, fake_client, state = build_test_app()
        with TypedTestClient(app) as http:
            fake_client.simulate_disconnect()
            asyncio.run(watch_terminal_once(state, backoff=_WATCHDOG_BACKOFF))

            fake_client.simulate_reconnect()
            changed = asyncio.run(watch_terminal_once(state, backoff=_WATCHDOG_BACKOFF))
            assert changed is True

            recovered = http.get("/v1/health")
            assert recovered.json()["terminal_connected"] is True

            accepted = http.post("/v1/orders", json=limit_order_body("co-watchdog-2"))
            assert accepted.json()["state"] == "ACKNOWLEDGED"


class TestClockDriftMeasurementIsWired:
    def test_excess_drift_is_reported_and_blocks_new_orders(self) -> None:
        app, _fake_client, state = build_test_app()
        with TypedTestClient(app) as http:
            baseline = http.get("/v1/health")
            assert baseline.json()["clock_drift_within_threshold"] is True

            asyncio.run(measure_clock_drift_once(state, probe=lambda: 10.0))

            after = http.get("/v1/health")
            assert after.json()["clock_drift_seconds"] == 10.0
            assert after.json()["clock_drift_within_threshold"] is False

            rejected = http.post("/v1/orders", json=limit_order_body("co-drift-1"))
            assert rejected.json()["state"] == "REJECTED"
            assert "clock drift" in rejected.json()["reason"]


class TestLimitsStateProviderIsWired:
    def test_the_orders_per_minute_limit_is_actually_enforced_from_the_journal(self) -> None:
        # A submission's own RECEIVED row is journaled before its limits
        # check runs, so that check already sees itself: max=2 is "the
        # first submission passes, the second is the one that trips it",
        # not "two submissions are allowed after this one".
        tight = LimitsConfig(
            symbol_allowlist=None,
            max_volume_per_order=permissive_limits_config().max_volume_per_order,
            max_orders_per_minute=2,
            max_open_notional=permissive_limits_config().max_open_notional,
            max_daily_loss=permissive_limits_config().max_daily_loss,
        )
        app, _fake_client, _state = build_test_app(limits_config=tight)
        with TypedTestClient(app) as http:
            first = http.post("/v1/orders", json=limit_order_body("co-lim-1"))
            second = http.post("/v1/orders", json=limit_order_body("co-lim-2"))

        assert first.json()["state"] == "ACKNOWLEDGED"
        assert second.json()["state"] == "REJECTED"
        assert "max_orders_per_minute" in second.json()["reason"]

    def test_the_symbol_allowlist_rule_is_enforced_through_a_live_submission(self) -> None:
        tight = LimitsConfig(
            symbol_allowlist=frozenset({"SOME_OTHER_SYMBOL"}),
            max_volume_per_order=permissive_limits_config().max_volume_per_order,
            max_orders_per_minute=permissive_limits_config().max_orders_per_minute,
            max_open_notional=permissive_limits_config().max_open_notional,
            max_daily_loss=permissive_limits_config().max_daily_loss,
        )
        app, _fake_client, _state = build_test_app(limits_config=tight)
        with TypedTestClient(app) as http:
            response = http.post("/v1/orders", json=limit_order_body("co-lim-allow-1"))

        assert response.json()["state"] == "REJECTED"
        assert "allowlist" in response.json()["reason"]

    def test_the_max_volume_per_order_rule_is_enforced_through_a_live_submission(self) -> None:
        tight = LimitsConfig(
            symbol_allowlist=None,
            max_volume_per_order=Decimal(1),
            max_orders_per_minute=permissive_limits_config().max_orders_per_minute,
            max_open_notional=permissive_limits_config().max_open_notional,
            max_daily_loss=permissive_limits_config().max_daily_loss,
        )
        app, _fake_client, _state = build_test_app(limits_config=tight)
        with TypedTestClient(app) as http:
            response = http.post(
                "/v1/orders",
                json=limit_order_body("co-lim-vol-1", volume="2"),
            )

        assert response.json()["state"] == "REJECTED"
        assert "max_volume_per_order" in response.json()["reason"]

    def test_the_max_open_notional_rule_is_enforced_through_a_live_submission(self) -> None:
        tight = LimitsConfig(
            symbol_allowlist=None,
            max_volume_per_order=permissive_limits_config().max_volume_per_order,
            max_orders_per_minute=permissive_limits_config().max_orders_per_minute,
            max_open_notional=Decimal(10),
            max_daily_loss=permissive_limits_config().max_daily_loss,
        )
        app, _fake_client, _state = build_test_app(limits_config=tight)
        with TypedTestClient(app) as http:
            response = http.post(
                "/v1/orders",
                json=limit_order_body("co-lim-notional-1", price="50", volume="1"),
            )

        assert response.json()["state"] == "REJECTED"
        assert "max_open_notional" in response.json()["reason"]

    def test_the_max_daily_loss_rule_is_enforced_through_a_live_submission(self) -> None:
        tight = LimitsConfig(
            symbol_allowlist=None,
            max_volume_per_order=permissive_limits_config().max_volume_per_order,
            max_orders_per_minute=permissive_limits_config().max_orders_per_minute,
            max_open_notional=permissive_limits_config().max_open_notional,
            max_daily_loss=Decimal(100),
        )
        app, _fake_client, state = build_test_app(limits_config=tight)
        # The tracker's baseline was seeded from the account at boot; a
        # later, lower snapshot (what the periodic equity loop would report)
        # is what actually makes daily_loss real instead of zero.
        state.equity_tracker.record(Decimal("99900.00"))

        with TypedTestClient(app) as http:
            response = http.post("/v1/orders", json=limit_order_body("co-lim-loss-1"))

        assert response.json()["state"] == "REJECTED"
        assert "max_daily_loss" in response.json()["reason"]


class TestCancelReplaceRemainderIsWired:
    def test_replace_after_a_partial_fill_only_asks_for_the_remaining_volume(self) -> None:
        app, fake_client, _state = build_test_app()
        with TypedTestClient(app) as http:
            submitted = http.post(
                "/v1/orders",
                json=limit_order_body("co-rep-1", volume="5"),
            )
            ticket = submitted.json()["ticket"]
            fake_client.partial_fill(ticket, Decimal(2))

            replaced = http.post(
                "/v1/orders/co-rep-1/replace",
                json={
                    "operation_id": "op-e2e-1",
                    "new_client_order_id": "co-rep-1-new",
                    "symbol": WINZ25,
                    "side": "BUY",
                    "type": "LIMIT",
                    "price": "60",
                    "volume": "5",
                    "time_in_force": "DAY",
                },
            )

        assert replaced.json()["outcome"] == "REPLACED"
        assert replaced.json()["new"]["volume"] == "3"
