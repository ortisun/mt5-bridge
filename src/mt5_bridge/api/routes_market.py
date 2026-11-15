"""GET /v1/health, /terminal, /account, /symbols, /bars, /ticks, /book.

``/bars`` and ``/ticks`` take a date range, never a quantity, and answer it
in pages (``core/history_paging.py``).
"""

from fastapi import APIRouter, Request

from mt5_bridge.api.app_state import get_state
from mt5_bridge.api.errors import SYMBOL_NOT_FOUND, VALIDATION_ERROR, ProblemError
from mt5_bridge.api.run_on_worker import run_on_worker
from mt5_bridge.api.schemas import (
    AccountResponse,
    BarResponse,
    BarsPageResponse,
    BookLevelResponse,
    HealthResponse,
    SymbolDetail,
    SymbolSummary,
    TerminalResponse,
    TickResponse,
    TicksPageResponse,
)
from mt5_bridge.core.clock_drift import within_threshold
from mt5_bridge.core.history_paging import (
    InvalidRangeError,
    plan_bar_pages,
    read_bar_page,
    read_tick_page,
    start_tick_page,
)
from mt5_bridge.core.trading_phase import classify_trading_phase
from mt5_bridge.core.worker import CommandPriority
from mt5_bridge.mt5.models import Bar, Tick, Timeframe

router = APIRouter(prefix="/v1", tags=["market"])


def _bar_response(bar: Bar) -> BarResponse:
    return BarResponse(
        symbol=bar.symbol,
        timeframe=bar.timeframe.value,
        time=bar.time,
        open=bar.open,
        high=bar.high,
        low=bar.low,
        close=bar.close,
        volume=bar.volume,
    )


def _tick_response(tick: Tick) -> TickResponse:
    return TickResponse(
        symbol=tick.symbol,
        time_msc=tick.time_msc,
        bid=tick.bid,
        ask=tick.ask,
        last=tick.last,
        volume=tick.volume,
    )


def _invalid_query(error: InvalidRangeError) -> ProblemError:
    return ProblemError(
        VALIDATION_ERROR,
        str(error),
        errors=[{"in": "query", "parameter": error.field, "detail": error.detail}],
    )


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
        raise ProblemError(SYMBOL_NOT_FOUND, f"unknown symbol {symbol!r}")
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


@router.get("/bars", response_model=BarsPageResponse)
async def bars(
    *,
    request: Request,
    symbol: str,
    timeframe: Timeframe,
    from_epoch: int,
    to_epoch: int,
    page: int = 1,
) -> BarsPageResponse:
    state = get_state(request)
    try:
        plan = plan_bar_pages(timeframe=timeframe, from_epoch=from_epoch, to_epoch=to_epoch)
        plan.window(page)
    except InvalidRangeError as error:
        raise _invalid_query(error) from error
    page_bars = await run_on_worker(
        state.worker,
        CommandPriority.MARKET_POLL,
        lambda c: read_bar_page(c, symbol, timeframe, plan, page),
    )
    return BarsPageResponse(
        symbol=symbol,
        timeframe=timeframe.value,
        from_epoch=from_epoch,
        to_epoch=to_epoch,
        page=page,
        total_pages=plan.total_pages,
        next_page=page + 1 if page < plan.total_pages else None,
        bars=[_bar_response(b) for b in page_bars],
    )


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
    return [_tick_response(tick)]


@router.get("/ticks", response_model=TicksPageResponse)
async def ticks(
    *,
    request: Request,
    symbol: str,
    from_time_msc: int,
    to_time_msc: int,
    cursor: str | None = None,
) -> TicksPageResponse:
    state = get_state(request)
    try:
        position = start_tick_page(
            from_time_msc=from_time_msc, to_time_msc=to_time_msc, cursor=cursor
        )
    except InvalidRangeError as error:
        raise _invalid_query(error) from error
    tick_page = await run_on_worker(
        state.worker,
        CommandPriority.MARKET_POLL,
        lambda c: read_tick_page(
            c,
            symbol,
            from_time_msc=from_time_msc,
            to_time_msc=to_time_msc,
            position=position,
        ),
    )
    next_cursor = tick_page.next_cursor
    return TicksPageResponse(
        symbol=symbol,
        from_time_msc=from_time_msc,
        to_time_msc=to_time_msc,
        next_cursor=None if next_cursor is None else next_cursor.encode(),
        ticks=[_tick_response(t) for t in tick_page.ticks],
    )


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
