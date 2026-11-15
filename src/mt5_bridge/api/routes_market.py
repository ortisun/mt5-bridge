"""GET /v1/health, /terminal, /account, /symbols, /bars, /ticks, /book."""

from fastapi import APIRouter, Request

from mt5_bridge.api.app_state import get_state
from mt5_bridge.api.errors import http_error
from mt5_bridge.api.run_on_worker import run_on_worker
from mt5_bridge.api.schemas import (
    AccountResponse,
    BarResponse,
    BookLevelResponse,
    HealthResponse,
    SymbolDetail,
    SymbolSummary,
    TerminalResponse,
    TickResponse,
)
from mt5_bridge.core.clock_drift import within_threshold
from mt5_bridge.core.trading_phase import classify_trading_phase
from mt5_bridge.core.worker import CommandPriority
from mt5_bridge.mt5.models import Timeframe

router = APIRouter(prefix="/v1", tags=["market"])


@router.get("/health", response_model=HealthResponse)
async def health(*, request: Request) -> HealthResponse:
    state = get_state(request)
    terminal = await run_on_worker(
        state.worker, CommandPriority.MARKET_POLL, lambda c: c.terminal_info()
    )
    kill_status = state.kill_switch.status()
    drift = state.clock_drift_state.drift_seconds
    last_poll = state.health.last_execution_poll_at
    execution_poll_latency_seconds = (
        (state.clock.now_utc() - last_poll).total_seconds() if last_poll is not None else None
    )
    return HealthResponse(
        status="ok",
        terminal_connected=terminal.connected,
        algo_trading_enabled=terminal.algo_trading_enabled,
        kill_switch_state=kill_status.state.value,
        clock_drift_seconds=drift,
        clock_drift_within_threshold=within_threshold(
            drift, state.settings.clock_drift_threshold_seconds
        ),
        auction_participation_supported=state.auction_participation_supported,
        tick_poll_latency_seconds=state.health.tick_poll_latency_seconds,
        bar_poll_latency_seconds=state.health.bar_poll_latency_seconds,
        execution_poll_latency_seconds=execution_poll_latency_seconds,
    )


@router.get("/terminal", response_model=TerminalResponse)
async def terminal_info(*, request: Request) -> TerminalResponse:
    state = get_state(request)
    info = await run_on_worker(
        state.worker, CommandPriority.MARKET_POLL, lambda c: c.terminal_info()
    )
    return TerminalResponse(
        connected=info.connected,
        algo_trading_enabled=info.algo_trading_enabled,
        build=info.build,
        name=info.name,
        company=info.company,
    )


@router.get("/account", response_model=AccountResponse)
async def account_info(*, request: Request) -> AccountResponse:
    state = get_state(request)
    account = await run_on_worker(
        state.worker, CommandPriority.MARKET_POLL, lambda c: c.account_info()
    )
    return AccountResponse(
        login=account.login,
        balance=account.balance,
        equity=account.equity,
        margin=account.margin,
        margin_free=account.margin_free,
        currency=account.currency,
        position_accounting=account.position_accounting,
        trade_allowed=account.trade_allowed,
    )


@router.get("/symbols", response_model=list[SymbolSummary])
async def list_symbols(
    *,
    request: Request,
    group: str | None = None,
) -> list[SymbolSummary]:
    state = get_state(request)
    symbols = await run_on_worker(
        state.worker, CommandPriority.MARKET_POLL, lambda c: c.symbols(group)
    )
    return [
        SymbolSummary(
            name=s.name,
            description=s.description,
            is_continuous_series=s.is_continuous_series,
            trading_allowed=s.trading_allowed,
        )
        for s in symbols
    ]


@router.get("/symbols/{symbol}", response_model=SymbolDetail)
async def symbol_detail(symbol: str, *, request: Request) -> SymbolDetail:
    state = get_state(request)
    info = await run_on_worker(
        state.worker, CommandPriority.MARKET_POLL, lambda c: c.symbol_info(symbol)
    )
    if info is None:
        raise http_error(404, code="NotFound", message=f"unknown symbol {symbol!r}")
    local_now = state.trading_day_policy.localize(state.clock.now_utc())
    phase = classify_trading_phase(info, now=local_now, auction_window=state.auction_window)
    return SymbolDetail(
        name=info.name,
        description=info.description,
        is_continuous_series=info.is_continuous_series,
        trading_allowed=info.trading_allowed,
        tick_size=info.tick_size,
        volume_min=info.volume_min,
        volume_max=info.volume_max,
        volume_step=info.volume_step,
        filling_modes=sorted(info.filling_modes, key=lambda m: m.value),
        time_in_force_modes=sorted(info.time_in_force_modes, key=lambda t: t.value),
        trading_phase=phase,
    )


@router.get("/bars", response_model=list[BarResponse])
async def bars(
    *,
    request: Request,
    symbol: str,
    timeframe: str,
    count: int = 100,
) -> list[BarResponse]:
    state = get_state(request)
    parsed_timeframe = Timeframe(timeframe)
    # start_pos=1: never even ask the client for the still-forming bar (rule 28).
    raw_bars = await run_on_worker(
        state.worker,
        CommandPriority.MARKET_POLL,
        lambda c: c.copy_bars(symbol, parsed_timeframe, 1, count),
    )
    return [
        BarResponse(
            symbol=b.symbol,
            timeframe=b.timeframe.value,
            time=b.time,
            open=b.open,
            high=b.high,
            low=b.low,
            close=b.close,
            volume=b.volume,
        )
        for b in raw_bars
        if b.is_closed  # defensive second check, never trust the source alone
    ]


@router.get("/ticks/latest", response_model=list[TickResponse])
async def latest_tick(*, request: Request, symbol: str) -> list[TickResponse]:
    """The terminal's current quote for ``symbol`` (``symbol_info_tick``): what a
    stream client reloads after a gap. Empty when the terminal has no quote."""
    state = get_state(request)
    tick = await run_on_worker(
        state.worker, CommandPriority.MARKET_POLL, lambda c: c.symbol_tick(symbol)
    )
    if tick is None:
        return []
    return [
        TickResponse(
            symbol=tick.symbol,
            time_msc=tick.time_msc,
            bid=tick.bid,
            ask=tick.ask,
            last=tick.last,
            volume=tick.volume,
        )
    ]


@router.get("/ticks", response_model=list[TickResponse])
async def ticks(
    *,
    request: Request,
    symbol: str,
    count: int = 100,
    from_time_msc: int | None = None,
) -> list[TickResponse]:
    state = get_state(request)
    # Omitting from_time_msc defaults to a recent window, not epoch 0 — the
    # oldest retained tick is rarely what a caller wants, and this bridge
    # keeps no long-term tick history (§114 "Role of Bridge Data"). Pass an
    # explicit from_time_msc (0 to page from the very oldest retained tick)
    # to look further back; count still caps the page either way.
    resolved_from_time_msc = (
        from_time_msc
        if from_time_msc is not None
        else state.clock.now_ms() - state.settings.ticks_default_lookback_seconds * 1000
    )
    raw_ticks = await run_on_worker(
        state.worker,
        CommandPriority.MARKET_POLL,
        lambda c: c.copy_ticks(symbol, resolved_from_time_msc, count),
    )
    return [
        TickResponse(
            symbol=t.symbol, time_msc=t.time_msc, bid=t.bid, ask=t.ask, last=t.last, volume=t.volume
        )
        for t in raw_ticks
    ]


@router.get("/book/{symbol}", response_model=list[BookLevelResponse])
async def book(symbol: str, *, request: Request) -> list[BookLevelResponse]:
    state = get_state(request)
    levels = await run_on_worker(
        state.worker, CommandPriority.MARKET_POLL, lambda c: c.order_book(symbol)
    )
    if levels is None:
        return []
    return [
        BookLevelResponse(side=level.side.value, price=level.price, volume=level.volume)
        for level in levels
    ]
