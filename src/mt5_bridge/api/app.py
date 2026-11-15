"""The FastAPI application factory. Every dependency is injected — no module-
level singletons — so tests build a fully isolated app per case."""

from collections.abc import AsyncGenerator, Callable
from contextlib import AbstractAsyncContextManager, asynccontextmanager

from fastapi import FastAPI, WebSocket

from mt5_bridge.api.app_state import AppState
from mt5_bridge.api.background import start_background_loops
from mt5_bridge.api.errors import install_error_handlers
from mt5_bridge.api.middleware import MaxBodySizeMiddleware
from mt5_bridge.api.routes_market import router as market_router
from mt5_bridge.api.routes_metrics import router as metrics_router
from mt5_bridge.api.routes_orders import router as orders_router
from mt5_bridge.api.telemetry import TelemetryMiddleware
from mt5_bridge.api.ws import run_stream_connection
from mt5_bridge.core.clock_drift import ClockDriftProbe, ntp_drift_probe


def create_app(
    state: AppState,
    *,
    start_background_loops_on_startup: bool = False,
    drift_probe: ClockDriftProbe | None = None,
) -> FastAPI:
    """``start_background_loops_on_startup`` is ``False`` by default so every
    existing test app stays free of real timers, real NTP calls, and a
    lifespan it never asked for; only ``main.py``'s real production
    entrypoint turns it on. Tests that exercise a periodic loop's behavior
    call the corresponding ``api.background.*_once`` function directly
    instead — deterministic, and still through this same running app. A test
    that instead exercises the *scheduler* itself passes a fake
    ``drift_probe`` so the clock-drift loop never performs real network I/O.
    """
    lifespan = (
        _lifespan(state, drift_probe or (lambda: ntp_drift_probe(state.clock)))
        if start_background_loops_on_startup
        else None
    )
    app = FastAPI(title="mt5-bridge", version="1.0.0", lifespan=lifespan)
    app.state.bridge = state
    app.add_middleware(MaxBodySizeMiddleware, max_bytes=state.settings.max_request_body_bytes)
    # Added last, so outermost: it also observes a request rejected above.
    app.add_middleware(TelemetryMiddleware, meter=state.meter)

    app.include_router(market_router)
    app.include_router(orders_router)
    app.include_router(metrics_router)

    install_error_handlers(app)

    @app.websocket("/v1/stream")
    async def stream(websocket: WebSocket) -> None:
        await run_stream_connection(
            websocket,
            account_id=state.expected_account_id,
            worker=state.worker,
            sequence_log=state.sequence_log,
            kill_switch=state.kill_switch,
            terminal_watchdog_state=state.terminal_watchdog_state,
            trading_day_policy=state.trading_day_policy,
            auction_window=state.auction_window,
            clock=state.clock,
        )

    return app


def _lifespan(
    state: AppState, drift_probe: ClockDriftProbe
) -> Callable[[FastAPI], AbstractAsyncContextManager[None]]:
    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncGenerator[None]:
        loops = start_background_loops(state, drift_probe=drift_probe)
        try:
            yield
        finally:
            await loops.stop()

    return lifespan
