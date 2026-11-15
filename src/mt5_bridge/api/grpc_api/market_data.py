"""The MarketData service: the gRPC form of ``api.routes_market``."""

from mt5_bridge.api.errors import SYMBOL_NOT_FOUND, ProblemError
from mt5_bridge.api.grpc_api import convert
from mt5_bridge.api.grpc_api.calls import Service, rpc
from mt5_bridge.api.grpc_api.generated import mt5_bridge_pb2 as pb
from mt5_bridge.api.grpc_api.generated import mt5_bridge_pb2_grpc as pb_grpc
from mt5_bridge.api.grpc_api.telemetry import Context
from mt5_bridge.api.run_on_worker import run_on_worker
from mt5_bridge.core.clock_drift import within_threshold
from mt5_bridge.core.trading_phase import classify_trading_phase
from mt5_bridge.core.worker import CommandPriority

DEFAULT_COUNT = 100


class MarketDataService(Service, pb_grpc.MarketDataServicer):
    service_name = pb.DESCRIPTOR.services_by_name["MarketData"].full_name

    @rpc
    async def GetHealth(self, request: pb.GetHealthRequest, context: Context) -> pb.Health:
        state = self.state
        terminal = await run_on_worker(
            state.worker, CommandPriority.MARKET_POLL, lambda c: c.terminal_info()
        )
        drift = state.clock_drift_state.drift_seconds
        last_poll = state.health.last_execution_poll_at
        return pb.Health(
            status="ok",
            terminal_connected=terminal.connected,
            algo_trading_enabled=terminal.algo_trading_enabled,
            kill_switch_state=convert.KILL_SWITCH_STATE.to_pb(state.kill_switch.status().state),
            clock_drift_seconds=drift,
            clock_drift_within_threshold=within_threshold(
                drift, state.settings.clock_drift_threshold_seconds
            ),
            auction_participation_supported=state.auction_participation_supported,
            tick_poll_latency_seconds=state.health.tick_poll_latency_seconds,
            bar_poll_latency_seconds=state.health.bar_poll_latency_seconds,
            execution_poll_latency_seconds=(
                None if last_poll is None else (state.clock.now_utc() - last_poll).total_seconds()
            ),
        )

    @rpc
    async def GetTerminal(self, request: pb.GetTerminalRequest, context: Context) -> pb.Terminal:
        state = self.state
        info = await run_on_worker(
            state.worker, CommandPriority.MARKET_POLL, lambda c: c.terminal_info()
        )
        return pb.Terminal(
            connected=info.connected,
            algo_trading_enabled=info.algo_trading_enabled,
            build=info.build,
            name=info.name,
            company=info.company,
        )

    @rpc
    async def GetAccount(self, request: pb.GetAccountRequest, context: Context) -> pb.Account:
        state = self.state
        account = await run_on_worker(
            state.worker, CommandPriority.MARKET_POLL, lambda c: c.account_info()
        )
        return pb.Account(
            login=account.login,
            balance=str(account.balance),
            equity=str(account.equity),
            margin=str(account.margin),
            margin_free=str(account.margin_free),
            currency=account.currency,
            position_accounting=convert.POSITION_ACCOUNTING.to_pb(account.position_accounting),
            trade_allowed=account.trade_allowed,
        )

    @rpc
    async def ListSymbols(
        self, request: pb.ListSymbolsRequest, context: Context
    ) -> pb.ListSymbolsResponse:
        state = self.state
        group = request.group if request.HasField("group") else None
        symbols = await run_on_worker(
            state.worker, CommandPriority.MARKET_POLL, lambda c: c.symbols(group)
        )
        return pb.ListSymbolsResponse(
            symbols=[
                pb.SymbolSummary(
                    name=s.name,
                    description=s.description,
                    is_continuous_series=s.is_continuous_series,
                    trading_allowed=s.trading_allowed,
                )
                for s in symbols
            ]
        )

    @rpc
    async def GetSymbol(self, request: pb.GetSymbolRequest, context: Context) -> pb.SymbolDetail:
        state = self.state
        symbol = request.symbol
        info = await run_on_worker(
            state.worker, CommandPriority.MARKET_POLL, lambda c: c.symbol_info(symbol)
        )
        if info is None:
            raise ProblemError(SYMBOL_NOT_FOUND, f"unknown symbol {symbol!r}")
        local_now = state.trading_day_policy.localize(state.clock.now_utc())
        phase = classify_trading_phase(info, now=local_now, auction_window=state.auction_window)
        return pb.SymbolDetail(
            name=info.name,
            description=info.description,
            is_continuous_series=info.is_continuous_series,
            trading_allowed=info.trading_allowed,
            tick_size=str(info.tick_size),
            volume_min=str(info.volume_min),
            volume_max=str(info.volume_max),
            volume_step=str(info.volume_step),
            filling_modes=[
                convert.FILLING_MODE.to_pb(m)
                for m in sorted(info.filling_modes, key=lambda m: m.value)
            ],
            time_in_force_modes=[
                convert.TIME_IN_FORCE.to_pb(t)
                for t in sorted(info.time_in_force_modes, key=lambda t: t.value)
            ],
            trading_phase=convert.TRADING_PHASE.to_pb(phase),
        )

    @rpc
    async def ListBars(self, request: pb.ListBarsRequest, context: Context) -> pb.ListBarsResponse:
        state = self.state
        symbol = request.symbol
        timeframe = convert.TIMEFRAME.from_pb(request.timeframe, field="timeframe")
        count = request.count if request.HasField("count") else DEFAULT_COUNT
        # start_pos=1: never even ask the terminal for the still-forming bar (rule 28).
        bars = await run_on_worker(
            state.worker,
            CommandPriority.MARKET_POLL,
            lambda c: c.copy_bars(symbol, timeframe, 1, count),
        )
        return pb.ListBarsResponse(
            bars=[
                pb.Bar(
                    symbol=b.symbol,
                    timeframe=convert.TIMEFRAME.to_pb(b.timeframe),
                    time=b.time,
                    open=str(b.open),
                    high=str(b.high),
                    low=str(b.low),
                    close=str(b.close),
                    volume=str(b.volume),
                )
                for b in bars
                if b.is_closed  # defensive second check, never trust the source alone
            ]
        )

    @rpc
    async def GetLatestTick(
        self, request: pb.GetLatestTickRequest, context: Context
    ) -> pb.GetLatestTickResponse:
        state = self.state
        symbol = request.symbol
        latest = await run_on_worker(
            state.worker, CommandPriority.MARKET_POLL, lambda c: c.symbol_tick(symbol)
        )
        return pb.GetLatestTickResponse(tick=None if latest is None else convert.tick(latest))

    @rpc
    async def ListTicks(
        self, request: pb.ListTicksRequest, context: Context
    ) -> pb.ListTicksResponse:
        state = self.state
        symbol = request.symbol
        count = request.count if request.HasField("count") else DEFAULT_COUNT
        # An absent from_time_msc is a recent window, not epoch 0: the bridge
        # keeps no long-term tick history (§114 "Role of Bridge Data").
        from_time_msc = (
            request.from_time_msc
            if request.HasField("from_time_msc")
            else state.clock.now_ms() - state.settings.ticks_default_lookback_seconds * 1000
        )
        ticks = await run_on_worker(
            state.worker,
            CommandPriority.MARKET_POLL,
            lambda c: c.copy_ticks(symbol, from_time_msc, count),
        )
        return pb.ListTicksResponse(ticks=[convert.tick(t) for t in ticks])

    @rpc
    async def GetBook(self, request: pb.GetBookRequest, context: Context) -> pb.GetBookResponse:
        state = self.state
        symbol = request.symbol
        levels = await run_on_worker(
            state.worker, CommandPriority.MARKET_POLL, lambda c: c.order_book(symbol)
        )
        return pb.GetBookResponse(
            levels=[
                pb.BookLevel(
                    side=convert.BOOK_SIDE.to_pb(level.side),
                    price=str(level.price),
                    volume=str(level.volume),
                )
                for level in levels or ()
            ]
        )
