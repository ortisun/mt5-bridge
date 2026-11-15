"""The ONLY module in this project allowed to import ``MetaTrader5`` (plan
rule 40; enforced by both projects' import-linter contracts).

UNVALIDATED: there is no Windows terminal in this environment to run it
against. It type-checks under Pyright strict on macOS against the minimal
stub in ``typings/MetaTrader5/`` (see that file's docstring for exactly
which fields and behaviors are modeled versus simplified); only its depth
path runs in a test, against a scripted stand-in for the terminal library
(``tests/mt5/test_real_client_book.py``). Every terminal call
follows the real API's own documented contract of "return ``None`` and set
``last_error()`` on failure" (mql5.com/en/docs/python_metatrader5, reviewed
2026-09-20 per plan §116) rather than raising; this client turns that into
``Mt5ConnectionLost`` so the rest of the bridge never special-cases it.

The single tested boundary from the terminal's native ``float`` to our
``Decimal`` (plan rule 33) is ``_dec`` below: it converts through ``str``
so the result matches the decimal literal a human would expect
(``Decimal(str(0.1))`` is ``Decimal('0.1')``; ``Decimal(0.1)`` is not) —
this conversion itself is correct by construction, but real-terminal
evidence that MT5's own floats behave as documented remains pending
(Windows/UTM smoke test, out of scope here).
"""

from datetime import UTC, datetime
from decimal import Decimal

import MetaTrader5

from mt5_bridge.mt5.client import Mt5Client
from mt5_bridge.mt5.errors import Mt5ConnectionLost
from mt5_bridge.mt5.models import (
    AccountInfo,
    AccountTradeMode,
    Bar,
    BookLevel,
    BookSide,
    DealEntry,
    FillingMode,
    ModifyOrderRequest,
    Mt5Credentials,
    Mt5Deal,
    Mt5ErrorInfo,
    Mt5Order,
    Mt5OrderState,
    Mt5Position,
    NewOrderIntent,
    OrderCheckResult,
    OrderSendResult,
    OrderSide,
    OrderType,
    PositionAccounting,
    SymbolInfo,
    TerminalAttachment,
    TerminalInfo,
    Tick,
    Timeframe,
    TimeInForce,
)
from mt5_bridge.mt5.retcodes import order_check_retcode, trade_retcode

_TIMEFRAME_TO_MT5: dict[Timeframe, int] = {
    Timeframe.M1: MetaTrader5.TIMEFRAME_M1,
    Timeframe.M5: MetaTrader5.TIMEFRAME_M5,
    Timeframe.M15: MetaTrader5.TIMEFRAME_M15,
    Timeframe.M30: MetaTrader5.TIMEFRAME_M30,
    Timeframe.H1: MetaTrader5.TIMEFRAME_H1,
    Timeframe.H4: MetaTrader5.TIMEFRAME_H4,
    Timeframe.D1: MetaTrader5.TIMEFRAME_D1,
    Timeframe.W1: MetaTrader5.TIMEFRAME_W1,
    Timeframe.MN1: MetaTrader5.TIMEFRAME_MN1,
}

_ORDER_TYPE_TO_MT5: dict[tuple[OrderSide, OrderType], int] = {
    (OrderSide.BUY, OrderType.MARKET): MetaTrader5.ORDER_TYPE_BUY,
    (OrderSide.SELL, OrderType.MARKET): MetaTrader5.ORDER_TYPE_SELL,
    (OrderSide.BUY, OrderType.LIMIT): MetaTrader5.ORDER_TYPE_BUY_LIMIT,
    (OrderSide.SELL, OrderType.LIMIT): MetaTrader5.ORDER_TYPE_SELL_LIMIT,
    (OrderSide.BUY, OrderType.STOP): MetaTrader5.ORDER_TYPE_BUY_STOP,
    (OrderSide.SELL, OrderType.STOP): MetaTrader5.ORDER_TYPE_SELL_STOP,
}
_MT5_TO_SIDE_AND_TYPE: dict[int, tuple[OrderSide, OrderType]] = {
    mt5_type: pair for pair, mt5_type in _ORDER_TYPE_TO_MT5.items()
}

_FILLING_TO_MT5: dict[FillingMode, int] = {
    FillingMode.FOK: MetaTrader5.ORDER_FILLING_FOK,
    FillingMode.IOC: MetaTrader5.ORDER_FILLING_IOC,
    FillingMode.RETURN: MetaTrader5.ORDER_FILLING_RETURN,
}
_MT5_TO_FILLING: dict[int, FillingMode] = {v: k for k, v in _FILLING_TO_MT5.items()}

_TIME_IN_FORCE_TO_MT5: dict[TimeInForce, int] = {
    TimeInForce.DAY: MetaTrader5.ORDER_TIME_DAY,
    TimeInForce.GTC: MetaTrader5.ORDER_TIME_GTC,
    TimeInForce.GTD: MetaTrader5.ORDER_TIME_SPECIFIED,
}
_MT5_TO_TIME_IN_FORCE: dict[int, TimeInForce] = {v: k for k, v in _TIME_IN_FORCE_TO_MT5.items()}

_ORDER_STATE_TO_MT5: dict[Mt5OrderState, int] = {
    Mt5OrderState.STARTED: MetaTrader5.ORDER_STATE_STARTED,
    Mt5OrderState.PLACED: MetaTrader5.ORDER_STATE_PLACED,
    Mt5OrderState.CANCELED: MetaTrader5.ORDER_STATE_CANCELED,
    Mt5OrderState.PARTIAL: MetaTrader5.ORDER_STATE_PARTIAL,
    Mt5OrderState.FILLED: MetaTrader5.ORDER_STATE_FILLED,
    Mt5OrderState.REJECTED: MetaTrader5.ORDER_STATE_REJECTED,
    Mt5OrderState.EXPIRED: MetaTrader5.ORDER_STATE_EXPIRED,
    Mt5OrderState.REQUEST_ADD: MetaTrader5.ORDER_STATE_REQUEST_ADD,
    Mt5OrderState.REQUEST_MODIFY: MetaTrader5.ORDER_STATE_REQUEST_MODIFY,
    Mt5OrderState.REQUEST_CANCEL: MetaTrader5.ORDER_STATE_REQUEST_CANCEL,
}
_MT5_TO_ORDER_STATE: dict[int, Mt5OrderState] = {v: k for k, v in _ORDER_STATE_TO_MT5.items()}

_DEAL_ENTRY_TO_MT5: dict[DealEntry, int] = {
    DealEntry.IN: MetaTrader5.DEAL_ENTRY_IN,
    DealEntry.OUT: MetaTrader5.DEAL_ENTRY_OUT,
    DealEntry.INOUT: MetaTrader5.DEAL_ENTRY_INOUT,
    DealEntry.OUT_BY: MetaTrader5.DEAL_ENTRY_OUT_BY,
}
_MT5_TO_DEAL_ENTRY: dict[int, DealEntry] = {v: k for k, v in _DEAL_ENTRY_TO_MT5.items()}

_MT5_POSITION_TYPE_TO_SIDE: dict[int, OrderSide] = {
    MetaTrader5.POSITION_TYPE_BUY: OrderSide.BUY,
    MetaTrader5.POSITION_TYPE_SELL: OrderSide.SELL,
}
_MT5_DEAL_TYPE_TO_SIDE: dict[int, OrderSide] = {
    MetaTrader5.DEAL_TYPE_BUY: OrderSide.BUY,
    MetaTrader5.DEAL_TYPE_SELL: OrderSide.SELL,
}

_TIMEFRAME_SECONDS: dict[Timeframe, int] = {
    Timeframe.M1: 60,
    Timeframe.M5: 5 * 60,
    Timeframe.M15: 15 * 60,
    Timeframe.M30: 30 * 60,
    Timeframe.H1: 60 * 60,
    Timeframe.H4: 4 * 60 * 60,
    Timeframe.D1: 24 * 60 * 60,
    Timeframe.W1: 7 * 24 * 60 * 60,
    # A calendar month has no fixed length; 31 days is a deliberate
    # overestimate so a still-forming MN1 bar is never marked closed early
    # (underclaiming "closed" only delays it appearing in results, which is
    # safe — see the module-level rule 28 note on ``copy_bars`` below).
    Timeframe.MN1: 31 * 24 * 60 * 60,
}


def _is_bar_closed(*, open_epoch: int, timeframe: Timeframe, now_epoch: int) -> bool:
    return open_epoch + _TIMEFRAME_SECONDS[timeframe] <= now_epoch


# An unrecognized trade mode is treated as REAL, so it can never pass a
# demo/paper boot check by accident.
_TRADE_MODE: dict[int, AccountTradeMode] = {
    MetaTrader5.ACCOUNT_TRADE_MODE_DEMO: AccountTradeMode.DEMO,
    MetaTrader5.ACCOUNT_TRADE_MODE_CONTEST: AccountTradeMode.CONTEST,
    MetaTrader5.ACCOUNT_TRADE_MODE_REAL: AccountTradeMode.REAL,
}

# A market-side entry (BOOK_TYPE_*_MARKET) is depth on the same side as its
# limit counterpart; the contract only distinguishes BUY from SELL.
_BOOK_TYPE_TO_SIDE: dict[int, BookSide] = {
    MetaTrader5.BOOK_TYPE_SELL: BookSide.SELL,
    MetaTrader5.BOOK_TYPE_SELL_MARKET: BookSide.SELL,
    MetaTrader5.BOOK_TYPE_BUY: BookSide.BUY,
    MetaTrader5.BOOK_TYPE_BUY_MARKET: BookSide.BUY,
}

_MARGIN_MODE_TO_ACCOUNTING: dict[int, PositionAccounting] = {
    MetaTrader5.ACCOUNT_MARGIN_MODE_RETAIL_NETTING: PositionAccounting.NETTING,
    MetaTrader5.ACCOUNT_MARGIN_MODE_RETAIL_HEDGING: PositionAccounting.HEDGING,
    # A B3-style exchange account nets positions per instrument like a
    # netting account; unvalidated against a real B3 account (§114).
    MetaTrader5.ACCOUNT_MARGIN_MODE_EXCHANGE: PositionAccounting.NETTING,
}


def _dec(value: float) -> Decimal:
    """The single tested boundary from native float to Decimal (rule 33)."""
    return Decimal(str(value))


def _require[T](value: T | None, what: str) -> T:
    if value is None:
        code, description = MetaTrader5.last_error()
        raise Mt5ConnectionLost(f"{what} failed: mt5_error=({code}, {description!r})")
    return value


def _side_and_type(mt5_type: int) -> tuple[OrderSide, OrderType]:
    pair = _MT5_TO_SIDE_AND_TYPE.get(mt5_type)
    if pair is None:
        raise Mt5ConnectionLost(f"unsupported MT5 order type {mt5_type}")
    return pair


class RealMt5Client:
    """Windows-only. See module docstring: unvalidated on this host."""

    def __init__(self) -> None:
        # Symbols whose depth this terminal session is already subscribed
        # to; a fresh initialize() starts a session with none.
        self._book_symbols: set[str] = set()

    def connect(self, credentials: Mt5Credentials) -> None:
        self._book_symbols.clear()
        ok = MetaTrader5.initialize(
            credentials.path,
            login=credentials.login,
            password=credentials.password,
            server=credentials.server,
        )
        if not ok:
            code, description = MetaTrader5.last_error()
            raise Mt5ConnectionLost(f"initialize failed: mt5_error=({code}, {description!r})")

    def attach(self, attachment: TerminalAttachment) -> None:
        # No login/password/server: initialize() attaches to the terminal
        # that is already running and logged in in this interactive session.
        self._book_symbols.clear()
        ok = MetaTrader5.initialize(attachment.path)
        if not ok:
            code, description = MetaTrader5.last_error()
            raise Mt5ConnectionLost(f"initialize failed: mt5_error=({code}, {description!r})")

    def disconnect(self) -> None:
        self._book_symbols.clear()
        MetaTrader5.shutdown()

    def terminal_info(self) -> TerminalInfo:
        info = _require(MetaTrader5.terminal_info(), "terminal_info")
        return TerminalInfo(
            connected=info.connected,
            algo_trading_enabled=info.trade_allowed,
            build=info.build,
            name=info.name,
            company=info.company,
        )

    def account_info(self) -> AccountInfo:
        info = _require(MetaTrader5.account_info(), "account_info")
        return AccountInfo(
            login=info.login,
            balance=_dec(info.balance),
            equity=_dec(info.equity),
            margin=_dec(info.margin),
            margin_free=_dec(info.margin_free),
            currency=info.currency,
            position_accounting=_MARGIN_MODE_TO_ACCOUNTING.get(
                info.margin_mode, PositionAccounting.NETTING
            ),
            trade_allowed=info.trade_allowed,
            server=info.server,
            trade_mode=_TRADE_MODE.get(info.trade_mode, AccountTradeMode.REAL),
        )

    def symbols(self, group: str | None = None) -> tuple[SymbolInfo, ...]:
        raw = MetaTrader5.symbols_get(group)
        if raw is None:
            return ()
        return tuple(self._convert_symbol(item) for item in raw)

    def symbol_info(self, symbol: str) -> SymbolInfo | None:
        raw = MetaTrader5.symbol_info(symbol)
        return None if raw is None else self._convert_symbol(raw)

    def _convert_symbol(self, raw: MetaTrader5.SymbolInfo) -> SymbolInfo:
        filling_modes = frozenset(
            mode for bit, mode in _MT5_TO_FILLING.items() if raw.filling_mode & bit
        )
        return SymbolInfo(
            name=raw.name,
            description=raw.description,
            currency_base=raw.currency_base,
            currency_profit=raw.currency_profit,
            currency_margin=raw.currency_margin,
            digits=raw.digits,
            tick_size=_dec(raw.trade_tick_size),
            tick_value=_dec(raw.trade_tick_value),
            volume_min=_dec(raw.volume_min),
            volume_max=_dec(raw.volume_max),
            volume_step=_dec(raw.volume_step),
            contract_size=_dec(raw.trade_contract_size),
            filling_modes=filling_modes,
            # The real API does not expose a discrete "supported TIF list"
            # the way it does filling modes; DAY is what the plan documents
            # as typical on an exchange venue (§114) and is the only value
            # exercised here — unvalidated pending real symbol metadata.
            time_in_force_modes=frozenset({TimeInForce.DAY}),
            is_continuous_series=False,
            trading_allowed=raw.visible,
            sessions=(),
        )

    def symbol_tick(self, symbol: str) -> Tick | None:
        raw = MetaTrader5.symbol_info_tick(symbol)
        if raw is None:
            return None
        return Tick(
            symbol=symbol,
            time_msc=raw.time_msc,
            bid=_dec(raw.bid),
            ask=_dec(raw.ask),
            last=_dec(raw.last),
            volume=_dec(raw.volume),
        )

    def copy_bars(
        self, symbol: str, timeframe: Timeframe, start_pos: int, count: int
    ) -> tuple[Bar, ...]:
        raw = MetaTrader5.copy_rates_from_pos(
            symbol, _TIMEFRAME_TO_MT5[timeframe], start_pos, count
        )
        if raw is None:
            return ()
        # ``start_pos >= 1`` already asks the terminal to skip the forming
        # bar, but rule 28 never trusts that alone (mirrors ``BarPoller``'s
        # own defensive filter): derive ``is_closed`` from this bar's own
        # close time against the terminal's server time (the latest tick),
        # never a hardcoded constant.
        now_epoch = self._server_now_epoch(symbol)
        bars: list[Bar] = []
        for row in raw:
            open_epoch = int(row["time"])  # type: ignore[call-overload]
            bars.append(
                Bar(
                    symbol=symbol,
                    timeframe=timeframe,
                    time=open_epoch,
                    open=_dec(float(row["open"])),  # type: ignore[call-overload]
                    high=_dec(float(row["high"])),  # type: ignore[call-overload]
                    low=_dec(float(row["low"])),  # type: ignore[call-overload]
                    close=_dec(float(row["close"])),  # type: ignore[call-overload]
                    volume=_dec(float(row["tick_volume"])),  # type: ignore[call-overload]
                    is_closed=_is_bar_closed(
                        open_epoch=open_epoch, timeframe=timeframe, now_epoch=now_epoch
                    ),
                )
            )
        return tuple(bars)

    def _server_now_epoch(self, symbol: str) -> int:
        """The terminal's own clock via the symbol's latest tick — never
        this process's clock, which is exactly what plan §114 clock-drift
        checking exists to distrust. Falls back to this process's clock only
        when no tick has ever arrived (e.g. a market that has not opened
        yet), which cannot make an already-elapsed bar close early."""
        tick = MetaTrader5.symbol_info_tick(symbol)
        if tick is not None:
            return int(tick.time)
        return int(datetime.now(UTC).timestamp())

    def copy_ticks(self, symbol: str, date_from_msc: int, count: int) -> tuple[Tick, ...]:
        # flags=2 is COPY_TICKS_ALL in the real API; kept as a local literal
        # since it is a wire-protocol constant, not a market/timeframe policy.
        raw = MetaTrader5.copy_ticks_from(symbol, date_from_msc // 1000, count, 2)
        if raw is None:
            return ()
        ticks: list[Tick] = []
        for row in raw:
            ticks.append(
                Tick(
                    symbol=symbol,
                    time_msc=int(row["time_msc"]),  # type: ignore[call-overload]
                    bid=_dec(float(row["bid"])),  # type: ignore[call-overload]
                    ask=_dec(float(row["ask"])),  # type: ignore[call-overload]
                    last=_dec(float(row["last"])),  # type: ignore[call-overload]
                    volume=_dec(float(row["volume"])),  # type: ignore[call-overload]
                )
            )
        return tuple(ticks)

    def order_book(self, symbol: str) -> tuple[BookLevel, ...] | None:
        # Depth is optional per the contract ("if supplied by the broker"):
        # a refused subscription (retried on the next poll, since depth may
        # start later) and a subscription whose first snapshot has not
        # arrived yet both mean "no depth right now", never an error.
        if symbol not in self._book_symbols:
            if not MetaTrader5.market_book_add(symbol):
                return None
            self._book_symbols.add(symbol)
        raw = MetaTrader5.market_book_get(symbol)
        if not raw:
            return None
        return (
            tuple(
                BookLevel(
                    side=_BOOK_TYPE_TO_SIDE[item.type],
                    price=_dec(item.price),
                    volume=_dec(item.volume_dbl),
                )
                for item in raw
                if item.type in _BOOK_TYPE_TO_SIDE
            )
            or None
        )

    def orders(self) -> tuple[Mt5Order, ...]:
        raw = MetaTrader5.orders_get()
        if raw is None:
            return ()
        return tuple(self._convert_order(item) for item in raw)

    def positions(self) -> tuple[Mt5Position, ...]:
        raw = MetaTrader5.positions_get()
        if raw is None:
            return ()
        positions: list[Mt5Position] = []
        for item in raw:
            side = _MT5_POSITION_TYPE_TO_SIDE.get(item.type)
            if side is None:
                raise Mt5ConnectionLost(f"unsupported MT5 position type {item.type}")
            positions.append(
                Mt5Position(
                    ticket=item.ticket,
                    symbol=item.symbol,
                    side=side,
                    volume=_dec(item.volume),
                    price_open=_dec(item.price_open),
                    price_current=_dec(item.price_current),
                    swap=_dec(item.swap),
                    profit=_dec(item.profit),
                    time_open=item.time,
                    magic=item.magic,
                    comment=item.comment,
                )
            )
        return tuple(positions)

    def history_orders(self, date_from_epoch: int, date_to_epoch: int) -> tuple[Mt5Order, ...]:
        raw = MetaTrader5.history_orders_get(date_from_epoch, date_to_epoch)
        if raw is None:
            return ()
        return tuple(self._convert_order(item) for item in raw)

    def history_deals(self, date_from_epoch: int, date_to_epoch: int) -> tuple[Mt5Deal, ...]:
        raw = MetaTrader5.history_deals_get(date_from_epoch, date_to_epoch)
        if raw is None:
            return ()
        deals: list[Mt5Deal] = []
        for item in raw:
            side = _MT5_DEAL_TYPE_TO_SIDE.get(item.type)
            if side is None:
                # A non-trade deal (balance operation, credit, correction,
                # ...): not a directional fill. Skipped rather than guessed.
                continue
            deals.append(
                Mt5Deal(
                    ticket=item.ticket,
                    order_ticket=item.order,
                    symbol=item.symbol,
                    side=side,
                    entry=_MT5_TO_DEAL_ENTRY.get(item.entry, DealEntry.IN),
                    volume=_dec(item.volume),
                    price=_dec(item.price),
                    commission=_dec(item.commission),
                    swap=_dec(item.swap),
                    profit=_dec(item.profit),
                    time_msc=item.time_msc,
                    magic=item.magic,
                    comment=item.comment,
                )
            )
        return tuple(deals)

    def _convert_order(self, raw: MetaTrader5.TradeOrder) -> Mt5Order:
        side, order_type = _side_and_type(raw.type)
        return Mt5Order(
            ticket=raw.ticket,
            symbol=raw.symbol,
            side=side,
            order_type=order_type,
            state=_MT5_TO_ORDER_STATE.get(raw.state, Mt5OrderState.STARTED),
            volume_initial=_dec(raw.volume_initial),
            volume_current=_dec(raw.volume_current),
            price_open=_dec(raw.price_open),
            stop_loss=_dec(raw.sl) if raw.sl else None,
            take_profit=_dec(raw.tp) if raw.tp else None,
            time_setup=raw.time_setup,
            time_expiration=raw.time_expiration or None,
            magic=raw.magic,
            comment=raw.comment,
            time_in_force=_MT5_TO_TIME_IN_FORCE.get(raw.type_time, TimeInForce.DAY),
            filling_mode=_MT5_TO_FILLING.get(raw.type_filling, FillingMode.RETURN),
        )

    def _build_request(self, intent: NewOrderIntent) -> dict[str, object]:
        is_market = intent.order_type is OrderType.MARKET
        request: dict[str, object] = {
            "action": MetaTrader5.TRADE_ACTION_DEAL
            if is_market
            else MetaTrader5.TRADE_ACTION_PENDING,
            "symbol": intent.symbol,
            "volume": float(intent.volume),
            "type": _ORDER_TYPE_TO_MT5[(intent.side, intent.order_type)],
            "type_filling": _FILLING_TO_MT5[intent.filling_mode],
            "type_time": _TIME_IN_FORCE_TO_MT5[intent.time_in_force],
            "magic": intent.magic,
            "comment": intent.comment,
        }
        if intent.price is not None:
            request["price"] = float(intent.price)
        if intent.stop_loss is not None:
            request["sl"] = float(intent.stop_loss)
        if intent.take_profit is not None:
            request["tp"] = float(intent.take_profit)
        if intent.expiration is not None:
            request["expiration"] = intent.expiration
        return request

    def order_check(self, intent: NewOrderIntent) -> OrderCheckResult:
        result = _require(MetaTrader5.order_check(self._build_request(intent)), "order_check")
        return OrderCheckResult(
            retcode=order_check_retcode(result.retcode),
            comment=result.comment,
            balance=_dec(result.balance),
            equity=_dec(result.equity),
            margin=_dec(result.margin),
            margin_free=_dec(result.margin_free),
        )

    def order_send(self, intent: NewOrderIntent) -> OrderSendResult:
        result = _require(MetaTrader5.order_send(self._build_request(intent)), "order_send")
        return OrderSendResult(
            retcode=trade_retcode(result.retcode, call="order_send"),
            order=result.order or None,
            deal=result.deal or None,
            volume=_dec(result.volume),
            price=_dec(result.price),
            comment=result.comment,
        )

    def modify_order(self, request: ModifyOrderRequest) -> OrderSendResult:
        raw_request: dict[str, object] = {
            "action": MetaTrader5.TRADE_ACTION_MODIFY,
            "order": request.ticket,
        }
        if request.price is not None:
            raw_request["price"] = float(request.price)
        if request.stop_loss is not None:
            raw_request["sl"] = float(request.stop_loss)
        if request.take_profit is not None:
            raw_request["tp"] = float(request.take_profit)
        if request.expiration is not None:
            raw_request["expiration"] = request.expiration
        result = _require(MetaTrader5.order_send(raw_request), "order_send(modify)")
        return OrderSendResult(
            retcode=trade_retcode(result.retcode, call="order_send(modify)"),
            order=result.order or None,
            deal=result.deal or None,
            volume=_dec(result.volume),
            price=_dec(result.price),
            comment=result.comment,
        )

    def cancel_order(self, ticket: int) -> OrderSendResult:
        request: dict[str, object] = {
            "action": MetaTrader5.TRADE_ACTION_REMOVE,
            "order": ticket,
        }
        result = _require(MetaTrader5.order_send(request), "order_send(remove)")
        return OrderSendResult(
            retcode=trade_retcode(result.retcode, call="order_send(remove)"),
            order=result.order or None,
            deal=result.deal or None,
            volume=_dec(result.volume),
            price=_dec(result.price),
            comment=result.comment,
        )

    def last_error(self) -> Mt5ErrorInfo:
        code, description = MetaTrader5.last_error()
        return Mt5ErrorInfo(code=code, description=description)


_static_protocol_check: Mt5Client = RealMt5Client()
"""A compile-time proof that ``RealMt5Client`` structurally satisfies
``Mt5Client``. Constructing it is side-effect-free (its ``__init__`` only
starts an empty depth-subscription set, so no terminal call happens);
nothing here ever calls one of its methods on this host."""
