"""A deterministic, in-memory terminal used by every test on macOS/Linux.

Implements ``Mt5Client`` well enough to exercise the bridge's real logic
(idempotent submission, cancel-replace races, closed-bar semantics, boot
reconciliation, disconnect handling) without a real terminal, a sleep, or the
network. Time only moves when the injected ``Clock`` moves; order/position
math is simplified where the simplification is documented and does not
affect the behaviors the bridge is responsible for (see ADR-025).
"""

import dataclasses
import itertools
from collections import deque
from collections.abc import Iterable
from decimal import Decimal

from mt5_bridge.clock import Clock, SystemClock
from mt5_bridge.mt5.errors import Mt5ConnectionLost
from mt5_bridge.mt5.models import (
    AccountInfo,
    Bar,
    BookLevel,
    DealEntry,
    ModifyOrderRequest,
    Mt5Credentials,
    Mt5Deal,
    Mt5ErrorInfo,
    Mt5Order,
    Mt5OrderState,
    Mt5Position,
    Mt5RetCode,
    NewOrderIntent,
    OrderCheckResult,
    OrderSendResult,
    OrderSide,
    OrderType,
    SymbolInfo,
    TerminalAttachment,
    TerminalInfo,
    Tick,
    Timeframe,
)

__all__ = ["FakeMt5Client", "Mt5ConnectionLost"]

_OrderOutcome = OrderSendResult | Exception


class FakeMt5Client:
    """Deterministic stand-in for a connected MT5 terminal.

    Test-control surface (not part of ``Mt5Client``): ``set_tick``,
    ``push_bar``, ``set_book``, ``force_fill``, ``simulate_disconnect``,
    ``simulate_reconnect``, ``queue_order_send_result``,
    ``queue_cancel_result``.
    """

    def __init__(
        self,
        *,
        symbols: Iterable[SymbolInfo],
        account: AccountInfo,
        clock: Clock | None = None,
    ) -> None:
        self._symbols: dict[str, SymbolInfo] = {symbol.name: symbol for symbol in symbols}
        self._account = account
        self._clock: Clock = clock or SystemClock()
        self._connected = False

        self._ticks: dict[str, Tick] = {}
        self._tick_history: dict[str, list[Tick]] = {}
        self._bars: dict[tuple[str, Timeframe], list[Bar]] = {}
        self._book: dict[str, tuple[BookLevel, ...] | None] = {}

        self._orders: dict[int, Mt5Order] = {}
        self._closed_at: dict[int, int] = {}
        self._deals: dict[int, Mt5Deal] = {}
        self._positions: dict[str, Mt5Position] = {}

        self._next_ticket = itertools.count(1000)
        self._next_deal = itertools.count(5000)
        self._last_error = Mt5ErrorInfo(code=0, description="no error")

        self._queued_order_send: deque[_OrderOutcome] = deque()
        self._queued_cancel: deque[_OrderOutcome] = deque()

    # ------------------------------------------------------------------
    # Mt5Client
    # ------------------------------------------------------------------

    def connect(self, credentials: Mt5Credentials) -> None:
        del credentials  # the fake never checks a password
        self._connected = True

    def attach(self, attachment: TerminalAttachment) -> None:
        del attachment  # the fake terminal is always "logged in"
        self._connected = True

    def disconnect(self) -> None:
        self._connected = False

    def terminal_info(self) -> TerminalInfo:
        return TerminalInfo(
            connected=self._connected,
            algo_trading_enabled=True,
            build=0,
            name="FakeTerminal",
            company="mt5-bridge",
        )

    def account_info(self) -> AccountInfo:
        self._ensure_connected()
        return self._account

    def symbols(self, group: str | None = None) -> tuple[SymbolInfo, ...]:
        self._ensure_connected()
        if group is None:
            return tuple(self._symbols.values())
        return tuple(s for s in self._symbols.values() if group in s.name)

    def symbol_info(self, symbol: str) -> SymbolInfo | None:
        self._ensure_connected()
        return self._symbols.get(symbol)

    def symbol_tick(self, symbol: str) -> Tick | None:
        self._ensure_connected()
        return self._ticks.get(symbol)

    def copy_bars(
        self, symbol: str, timeframe: Timeframe, start_pos: int, count: int
    ) -> tuple[Bar, ...]:
        self._ensure_connected()
        ordered = self._bars.get((symbol, timeframe), [])
        end = len(ordered) - start_pos
        if end <= 0:
            return ()
        begin = max(0, end - count)
        return tuple(ordered[begin:end])

    def copy_bars_range(
        self, symbol: str, timeframe: Timeframe, date_from_epoch: int, date_to_epoch: int
    ) -> tuple[Bar, ...]:
        self._ensure_connected()
        return tuple(
            bar
            for bar in self._bars.get((symbol, timeframe), [])
            if date_from_epoch <= bar.time < date_to_epoch
        )

    def copy_ticks(self, symbol: str, date_from_msc: int, count: int) -> tuple[Tick, ...]:
        self._ensure_connected()
        history = self._tick_history.get(symbol, [])
        matching = [tick for tick in history if tick.time_msc >= date_from_msc]
        return tuple(matching[:count])

    def order_book(self, symbol: str) -> tuple[BookLevel, ...] | None:
        self._ensure_connected()
        return self._book.get(symbol)

    def orders(self) -> tuple[Mt5Order, ...]:
        self._ensure_connected()
        return tuple(o for ticket, o in self._orders.items() if ticket not in self._closed_at)

    def positions(self) -> tuple[Mt5Position, ...]:
        self._ensure_connected()
        return tuple(self._positions.values())

    def history_orders(self, date_from_epoch: int, date_to_epoch: int) -> tuple[Mt5Order, ...]:
        self._ensure_connected()
        result: list[Mt5Order] = []
        for ticket, order in self._orders.items():
            end_time = self._closed_at.get(ticket, order.time_setup)
            if order.time_setup > date_to_epoch or end_time < date_from_epoch:
                continue
            result.append(order)
        return tuple(result)

    def history_deals(self, date_from_epoch: int, date_to_epoch: int) -> tuple[Mt5Deal, ...]:
        self._ensure_connected()
        lo, hi = date_from_epoch * 1000, date_to_epoch * 1000
        return tuple(d for d in self._deals.values() if lo <= d.time_msc <= hi)

    def order_check(self, intent: NewOrderIntent) -> OrderCheckResult:
        self._ensure_connected()
        retcode, reason = self._validate(intent)
        return OrderCheckResult(
            retcode=retcode,
            comment=reason,
            balance=self._account.balance,
            equity=self._account.equity,
            margin=self._account.margin,
            margin_free=self._account.margin_free,
        )

    def order_send(self, intent: NewOrderIntent) -> OrderSendResult:
        self._ensure_connected()
        if self._queued_order_send:
            return self._pop_queued(self._queued_order_send)

        retcode, reason = self._validate(intent)
        if retcode not in (Mt5RetCode.DONE, Mt5RetCode.PLACED):
            self._last_error = Mt5ErrorInfo(code=int(retcode), description=reason)
            return OrderSendResult(
                retcode=retcode,
                order=None,
                deal=None,
                volume=intent.volume,
                price=intent.price or Decimal(0),
                comment=reason,
            )

        ticket = next(self._next_ticket)
        now = self._clock.now_ms() // 1000

        if intent.order_type is OrderType.MARKET:
            tick = self._ticks.get(intent.symbol)
            fallback = intent.price or Decimal(0)
            fill_price = (
                (tick.ask if intent.side is OrderSide.BUY else tick.bid) if tick else fallback
            )
            order = Mt5Order(
                ticket=ticket,
                symbol=intent.symbol,
                side=intent.side,
                order_type=intent.order_type,
                state=Mt5OrderState.FILLED,
                volume_initial=intent.volume,
                volume_current=Decimal(0),
                price_open=fill_price,
                stop_loss=intent.stop_loss,
                take_profit=intent.take_profit,
                time_setup=now,
                time_expiration=intent.expiration,
                magic=intent.magic,
                comment=intent.comment,
                time_in_force=intent.time_in_force,
                filling_mode=intent.filling_mode,
            )
            self._orders[ticket] = order
            self._closed_at[ticket] = now
            deal_ticket = self._record_deal(order, fill_price, intent.volume)
            self._apply_position(order, fill_price, intent.volume)
            return OrderSendResult(
                retcode=Mt5RetCode.DONE,
                order=ticket,
                deal=deal_ticket,
                volume=intent.volume,
                price=fill_price,
                comment="filled",
            )

        order = Mt5Order(
            ticket=ticket,
            symbol=intent.symbol,
            side=intent.side,
            order_type=intent.order_type,
            state=Mt5OrderState.PLACED,
            volume_initial=intent.volume,
            volume_current=intent.volume,
            price_open=intent.price or Decimal(0),
            stop_loss=intent.stop_loss,
            take_profit=intent.take_profit,
            time_setup=now,
            time_expiration=intent.expiration,
            magic=intent.magic,
            comment=intent.comment,
            time_in_force=intent.time_in_force,
            filling_mode=intent.filling_mode,
        )
        self._orders[ticket] = order
        return OrderSendResult(
            retcode=Mt5RetCode.PLACED,
            order=ticket,
            deal=None,
            volume=intent.volume,
            price=order.price_open,
            comment="placed",
        )

    def modify_order(self, request: ModifyOrderRequest) -> OrderSendResult:
        self._ensure_connected()
        order = self._orders.get(request.ticket)
        if order is None or request.ticket in self._closed_at:
            return OrderSendResult(
                retcode=Mt5RetCode.INVALID,
                order=request.ticket if order is not None else None,
                deal=None,
                volume=Decimal(0),
                price=Decimal(0),
                comment="order not open",
            )
        updated = dataclasses.replace(
            order,
            price_open=request.price if request.price is not None else order.price_open,
            stop_loss=request.stop_loss if request.stop_loss is not None else order.stop_loss,
            take_profit=(
                request.take_profit if request.take_profit is not None else order.take_profit
            ),
            time_expiration=(
                request.expiration if request.expiration is not None else order.time_expiration
            ),
        )
        self._orders[request.ticket] = updated
        return OrderSendResult(
            retcode=Mt5RetCode.DONE,
            order=request.ticket,
            deal=None,
            volume=updated.volume_current,
            price=updated.price_open,
            comment="modified",
        )

    def cancel_order(self, ticket: int) -> OrderSendResult:
        self._ensure_connected()
        if self._queued_cancel:
            return self._pop_queued(self._queued_cancel)

        order = self._orders.get(ticket)
        if order is None:
            return OrderSendResult(
                retcode=Mt5RetCode.INVALID,
                order=None,
                deal=None,
                volume=Decimal(0),
                price=Decimal(0),
                comment="unknown ticket",
            )
        if ticket in self._closed_at:
            if order.state is Mt5OrderState.CANCELED:
                return OrderSendResult(
                    retcode=Mt5RetCode.DONE,
                    order=ticket,
                    deal=None,
                    volume=Decimal(0),
                    price=order.price_open,
                    comment="already canceled",
                )
            return OrderSendResult(
                retcode=Mt5RetCode.REJECT,
                order=ticket,
                deal=None,
                volume=Decimal(0),
                price=order.price_open,
                comment="order already finalized",
            )

        now = self._clock.now_ms() // 1000
        # The history order keeps whatever remained unfilled at the moment
        # of cancellation (never forced to zero), but the result itself
        # carries no dealt volume — exactly like the terminal's
        # TRADE_ACTION_REMOVE result (075 demo evidence). Anything filled
        # before the cancel is only visible in the deals.
        canceled = dataclasses.replace(order, state=Mt5OrderState.CANCELED)
        self._orders[ticket] = canceled
        self._closed_at[ticket] = now
        return OrderSendResult(
            retcode=Mt5RetCode.DONE,
            order=ticket,
            deal=None,
            volume=Decimal(0),
            price=order.price_open,
            comment="canceled",
        )

    def last_error(self) -> Mt5ErrorInfo:
        return self._last_error

    # ------------------------------------------------------------------
    # Test-control surface
    # ------------------------------------------------------------------

    def simulate_disconnect(self) -> None:
        """Model the terminal dropping out from under the bridge."""
        self._connected = False

    def simulate_reconnect(self) -> None:
        self._connected = True

    def set_tick(
        self, symbol: str, *, bid: Decimal, ask: Decimal, last: Decimal, volume: Decimal
    ) -> None:
        tick = Tick(
            symbol=symbol, time_msc=self._clock.now_ms(), bid=bid, ask=ask, last=last, volume=volume
        )
        self._ticks[symbol] = tick
        self._tick_history.setdefault(symbol, []).append(tick)

    def set_book(self, symbol: str, levels: tuple[BookLevel, ...] | None) -> None:
        self._book[symbol] = levels

    def push_bar(
        self,
        symbol: str,
        timeframe: Timeframe,
        *,
        time: int,
        o: Decimal | int,
        h: Decimal | int,
        low: Decimal | int,
        c: Decimal | int,
        volume: Decimal | int,
    ) -> None:
        """Close the current forming bar (if any) and open a new one."""
        key = (symbol, timeframe)
        series = self._bars.setdefault(key, [])
        if series and not series[-1].is_closed:
            series[-1] = dataclasses.replace(series[-1], is_closed=True)
        series.append(
            Bar(
                symbol=symbol,
                timeframe=timeframe,
                time=time,
                open=Decimal(o),
                high=Decimal(h),
                low=Decimal(low),
                close=Decimal(c),
                volume=Decimal(volume),
                is_closed=False,
            )
        )

    def partial_fill(
        self, ticket: int, filled_volume: Decimal, price: Decimal | None = None
    ) -> None:
        """Fill part of a resting order, leaving the remainder open — models
        a partial fill landing before (or racing) a cancel/replace request,
        so cancel-replace remainder math has something real to compute
        against (plan §114/§56)."""
        order = self._orders[ticket]
        if filled_volume <= 0 or filled_volume > order.volume_current:
            raise ValueError(
                "filled_volume must be positive and at most the order's volume_current"
            )
        fill_price = price if price is not None else order.price_open
        updated = dataclasses.replace(
            order,
            state=Mt5OrderState.PARTIAL,
            volume_current=order.volume_current - filled_volume,
        )
        self._orders[ticket] = updated
        self._record_deal(updated, fill_price, filled_volume)
        self._apply_position(updated, fill_price, filled_volume)

    def force_fill(self, ticket: int, price: Decimal | None = None) -> None:
        """Fill a resting order out of band, modeling a concurrent fill that
        races a cancel/replace request already in flight."""
        order = self._orders[ticket]
        fill_price = price if price is not None else order.price_open
        filled = dataclasses.replace(order, state=Mt5OrderState.FILLED, volume_current=Decimal(0))
        self._orders[ticket] = filled
        now = self._clock.now_ms() // 1000
        self._closed_at[ticket] = now
        self._record_deal(filled, fill_price, order.volume_current)
        self._apply_position(filled, fill_price, order.volume_current)

    def queue_order_send_result(self, outcome: _OrderOutcome) -> None:
        self._queued_order_send.append(outcome)

    def queue_cancel_result(self, outcome: _OrderOutcome) -> None:
        self._queued_cancel.append(outcome)

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _ensure_connected(self) -> None:
        if not self._connected:
            raise Mt5ConnectionLost("terminal is not connected")

    @staticmethod
    def _pop_queued(queue: deque[_OrderOutcome]) -> OrderSendResult:
        outcome = queue.popleft()
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    def _validate(self, intent: NewOrderIntent) -> tuple[Mt5RetCode, str]:
        symbol = self._symbols.get(intent.symbol)
        if symbol is None:
            return Mt5RetCode.INVALID, "unknown symbol"
        if symbol.is_continuous_series:
            return (
                Mt5RetCode.INVALID,
                "continuous series symbols are not tradable; use the dated contract",
            )
        if intent.volume < symbol.volume_min or intent.volume > symbol.volume_max:
            return Mt5RetCode.INVALID_VOLUME, "volume outside the symbol's [min, max]"
        if (intent.volume % symbol.volume_step) != 0:
            return Mt5RetCode.INVALID_VOLUME, "volume is not a multiple of the volume step"
        if intent.filling_mode not in symbol.filling_modes:
            return Mt5RetCode.INVALID_FILL, "filling mode not supported for this symbol"
        if intent.time_in_force not in symbol.time_in_force_modes:
            return Mt5RetCode.INVALID, "time in force not supported for this symbol"
        if intent.order_type is not OrderType.MARKET and intent.price is None:
            return Mt5RetCode.INVALID_PRICE, "price is required for non-market orders"
        if intent.order_type is OrderType.MARKET:
            return Mt5RetCode.DONE, "ok"
        return Mt5RetCode.PLACED, "ok"

    def _record_deal(self, order: Mt5Order, price: Decimal, volume: Decimal) -> int:
        deal_ticket = next(self._next_deal)
        self._deals[deal_ticket] = Mt5Deal(
            ticket=deal_ticket,
            order_ticket=order.ticket,
            symbol=order.symbol,
            side=order.side,
            entry=DealEntry.IN,
            volume=volume,
            price=price,
            commission=Decimal(0),
            swap=Decimal(0),
            profit=Decimal(0),
            time_msc=self._clock.now_ms(),
            magic=order.magic,
            comment=order.comment,
        )
        return deal_ticket

    def _apply_position(self, order: Mt5Order, price: Decimal, volume: Decimal) -> None:
        signed = volume if order.side is OrderSide.BUY else -volume
        existing = self._positions.get(order.symbol)
        if existing is None:
            if signed == 0:
                return
            side = OrderSide.BUY if signed > 0 else OrderSide.SELL
            self._positions[order.symbol] = Mt5Position(
                ticket=order.ticket,
                symbol=order.symbol,
                side=side,
                volume=abs(signed),
                price_open=price,
                price_current=price,
                swap=Decimal(0),
                profit=Decimal(0),
                time_open=order.time_setup,
                magic=order.magic,
                comment=order.comment,
            )
            return

        existing_signed = existing.volume if existing.side is OrderSide.BUY else -existing.volume
        new_signed = existing_signed + signed
        if new_signed == 0:
            del self._positions[order.symbol]
            return
        # Simplification (documented in ADR-025): a fill that flips the net
        # side does not blend the average price across the sign crossing.
        new_side = OrderSide.BUY if new_signed > 0 else OrderSide.SELL
        self._positions[order.symbol] = Mt5Position(
            ticket=order.ticket,
            symbol=order.symbol,
            side=new_side,
            volume=abs(new_signed),
            price_open=price,
            price_current=price,
            swap=existing.swap,
            profit=existing.profit,
            time_open=existing.time_open,
            magic=order.magic,
            comment=order.comment,
        )
