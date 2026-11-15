"""The FastAPI application factory. Every dependency is injected — no module-
level singletons — so tests build a fully isolated app per case."""

from collections.abc import AsyncGenerator, Callable
from contextlib import AbstractAsyncContextManager, asynccontextmanager

from fastapi import FastAPI, WebSocket
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from mt5_bridge.api.app_state import AppState
from mt5_bridge.api.background import start_background_loops
from mt5_bridge.api.errors import (
    error_body,
    handle_http_exception,
    handle_idempotency_conflict,
    handle_order_not_found,
    handle_value_error,
    handle_worker_unavailable,
)
from mt5_bridge.api.middleware import MaxBodySizeMiddleware
from mt5_bridge.api.routes_market import router as market_router
from mt5_bridge.api.routes_metrics import router as metrics_router
from mt5_bridge.api.routes_orders import router as orders_router
from mt5_bridge.api.ws import run_stream_connection
from mt5_bridge.core.clock_drift import ClockDriftProbe, ntp_drift_probe
from mt5_bridge.core.order_service import IdempotencyConflict, OrderNotFound, WorkerUnavailable


async def _handle_validation_error(request: object, exc: Exception) -> JSONResponse:
    del request
    if not isinstance(
        exc, RequestValidationError
    ):  # pragma: no cover - handler is scoped to this type
        raise exc
    return JSONResponse(
        status_code=422, content=error_body(code="ValidationError", message=str(exc.errors()))
    )


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

    app.include_router(market_router)
    app.include_router(orders_router)
    app.include_router(metrics_router)

    app.add_exception_handler(StarletteHTTPException, handle_http_exception)
    app.add_exception_handler(IdempotencyConflict, handle_idempotency_conflict)
    app.add_exception_handler(OrderNotFound, handle_order_not_found)
    app.add_exception_handler(ValueError, handle_value_error)
    app.add_exception_handler(WorkerUnavailable, handle_worker_unavailable)
    app.add_exception_handler(RequestValidationError, _handle_validation_error)

    @app.websocket("/v1/stream")
    async def stream(websocket: WebSocket) -> None:
        await run_stream_connection(
            websocket,
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
