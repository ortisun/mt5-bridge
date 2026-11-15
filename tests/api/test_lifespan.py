"""Proves the ASGI lifespan really schedules the background loops (as
opposed to the ``*_once`` functions merely existing and being directly
callable, which ``test_background_wiring.py`` already covers in depth).
Runs on the test's own event loop via Starlette's ``lifespan_context`` so the
bounded wait below and the scheduled loops share one loop deterministically
— no cross-thread timing, no real network I/O (the drift probe is faked).
"""

import asyncio
import dataclasses

import grpc
import pytest

from mt5_bridge.api.app import create_app
from mt5_bridge.api.grpc_api.generated import mt5_bridge_pb2 as pb
from mt5_bridge.api.grpc_api.generated import mt5_bridge_pb2_grpc as pb_grpc
from mt5_bridge.api.grpc_api.server import GrpcServeOptions

from testkit.app_builder import build_test_app_state


@pytest.mark.asyncio
async def test_the_lifespan_schedules_every_background_loop() -> None:
    state, _fake_client = build_test_app_state()
    fast_settings = dataclasses.replace(
        state.settings,
        execution_poll_interval_seconds=0.01,
        dead_man_sweep_interval_seconds=0.01,
        clock_drift_probe_interval_seconds=0.01,
        equity_snapshot_interval_seconds=0.01,
        watchdog_poll_interval_seconds=0.01,
    )
    fast_state = dataclasses.replace(state, settings=fast_settings)
    app = create_app(fast_state, start_background_loops_on_startup=True, drift_probe=lambda: 0.25)

    async with app.router.lifespan_context(app):
        loop_names = {
            task.get_name()
            for task in asyncio.all_tasks()
            if task.get_name().startswith("mt5-bridge-")
        }
        assert loop_names == {
            "mt5-bridge-execution-poll",
            "mt5-bridge-dead-man-sweep",
            "mt5-bridge-clock-drift",
            "mt5-bridge-equity-snapshot",
            "mt5-bridge-terminal-watchdog",
        }
        for _ in range(200):  # bounded wait: up to ~2s for a 10ms-interval loop
            if fast_state.health.last_execution_poll_at is not None:
                break
            await asyncio.sleep(0.01)

    assert fast_state.health.last_execution_poll_at is not None
    assert fast_state.clock_drift_state.drift_seconds == 0.25
    assert fast_state.equity_tracker.current_equity is not None


@pytest.mark.asyncio
async def test_the_lifespan_serves_grpc_while_the_app_runs() -> None:
    state, _fake_client = build_test_app_state()
    app = create_app(state, grpc_options=GrpcServeOptions(host="127.0.0.1", port=0))

    async with app.router.lifespan_context(app):
        port: int = app.state.grpc_port
        async with grpc.aio.insecure_channel(f"127.0.0.1:{port}") as channel:
            account = await pb_grpc.MarketDataStub(channel).GetAccount(pb.GetAccountRequest())
        # Only gRPC was asked for: no background loop was started with it.
        loops = [t for t in asyncio.all_tasks() if t.get_name().startswith("mt5-bridge-")]

    assert account.login == int(state.expected_account_id)
    assert loops == []
    async with grpc.aio.insecure_channel(f"127.0.0.1:{port}") as channel:
        with pytest.raises(grpc.aio.AioRpcError) as stopped:
            await pb_grpc.MarketDataStub(channel).GetAccount(pb.GetAccountRequest(), timeout=2)
    # A refused connection is UNAVAILABLE where the system refuses at once; where it
    # does not (Windows), the call runs into its deadline instead.
    assert stopped.value.code() in {
        grpc.StatusCode.UNAVAILABLE,
        grpc.StatusCode.DEADLINE_EXCEEDED,
    }
