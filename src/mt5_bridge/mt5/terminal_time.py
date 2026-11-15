"""Terminals that report their server's wall clock as if it were UTC.

The MetaTrader5 Python API documents tick, bar, order and deal epochs as UTC
(plan §116), and ``core/time_normalizer.py`` relies on that. The M8/069
Windows/UTM smoke test proved a deviation for the Rico-DEMO server (build
6182): B3's first M1 bar of 2026-09-25 carries the epoch of 10:03 "UTC",
while the session opens at 10:00 America/Sao_Paulo (13:00 UTC), so the
terminal reports its server's wall clock. Plan §114 requires a proven
deviation to get an explicit, versioned policy rather than an inferred
offset: ``ServerWallClock`` names the server's IANA zone (configuration,
never guessed from magnitudes or the current clock), and
``ServerWallClockTerminal`` applies it at the one boundary where terminal
times enter and leave the bridge, so every other module sees true UTC
exactly once and nothing converts twice. It also stamps the ``Tick``/``Bar``
it returns with the epoch the terminal actually reported
(``source_time_msc``/``source_time``) before overwriting ``time_msc``/
``time`` with the converted value, so a WS envelope's ``source_time_raw``
can still show what the terminal said rather than what the bridge computed.
"""

import dataclasses
from datetime import UTC, datetime
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from mt5_bridge.mt5.client import Mt5Client
from mt5_bridge.mt5.models import (
    AccountInfo,
    Bar,
    BookLevel,
    ModifyOrderRequest,
    Mt5Credentials,
    Mt5Deal,
    Mt5ErrorInfo,
    Mt5Order,
    Mt5Position,
    NewOrderIntent,
    OrderCheckResult,
    OrderSendResult,
    SymbolInfo,
    TerminalAttachment,
    TerminalInfo,
    Tick,
    Timeframe,
)


class ServerWallClock:
    """Maps a terminal's wall-clock epochs in ``zone`` to and from true UTC."""

    def __init__(self, zone: str) -> None:
        try:
            self._zone = ZoneInfo(zone)
        except (ZoneInfoNotFoundError, ValueError) as error:
            raise ValueError(f"unknown terminal server time zone {zone!r}") from error
        self.zone_name = zone

    def to_utc_seconds(self, wall_epoch: int) -> int:
        wall = datetime.fromtimestamp(wall_epoch, UTC).replace(tzinfo=self._zone)
        return int(wall.timestamp())

    def to_wall_seconds(self, utc_epoch: int) -> int:
        local = datetime.fromtimestamp(utc_epoch, UTC).astimezone(self._zone)
        return int(local.replace(tzinfo=UTC).timestamp())

    def to_utc_msc(self, wall_msc: int) -> int:
        seconds, millis = divmod(wall_msc, 1000)
        return self.to_utc_seconds(seconds) * 1000 + millis

    def to_wall_msc(self, utc_msc: int) -> int:
        seconds, millis = divmod(utc_msc, 1000)
        return self.to_wall_seconds(seconds) * 1000 + millis


class ServerWallClockTerminal:
    """``Mt5Client`` decorator: terminal wall-clock epochs in, true UTC out."""

    def __init__(self, inner: Mt5Client, clock: ServerWallClock) -> None:
        self._inner = inner
        self._clock = clock

    def _tick(self, tick: Tick) -> Tick:
        return dataclasses.replace(
            tick,
            time_msc=self._clock.to_utc_msc(tick.time_msc),
            source_time_msc=tick.time_msc,
        )

    def _order(self, order: Mt5Order) -> Mt5Order:
        expiration = order.time_expiration
        return dataclasses.replace(
            order,
            time_setup=self._clock.to_utc_seconds(order.time_setup),
            time_expiration=None if expiration is None else self._clock.to_utc_seconds(expiration),
        )

    def _outbound_expiration(self, expiration: int | None) -> int | None:
        return None if expiration is None else self._clock.to_wall_seconds(expiration)

    def connect(self, credentials: Mt5Credentials) -> None:
        self._inner.connect(credentials)

    def attach(self, attachment: TerminalAttachment) -> None:
        self._inner.attach(attachment)

    def disconnect(self) -> None:
        self._inner.disconnect()

    def terminal_info(self) -> TerminalInfo:
        return self._inner.terminal_info()

    def account_info(self) -> AccountInfo:
        return self._inner.account_info()

    def symbols(self, group: str | None = None) -> tuple[SymbolInfo, ...]:
        return self._inner.symbols(group)

    def symbol_info(self, symbol: str) -> SymbolInfo | None:
        return self._inner.symbol_info(symbol)

    def symbol_tick(self, symbol: str) -> Tick | None:
        tick = self._inner.symbol_tick(symbol)
        return None if tick is None else self._tick(tick)

    def copy_bars(
        self, symbol: str, timeframe: Timeframe, start_pos: int, count: int
    ) -> tuple[Bar, ...]:
        return tuple(
            dataclasses.replace(
                bar, time=self._clock.to_utc_seconds(bar.time), source_time=bar.time
            )
            for bar in self._inner.copy_bars(symbol, timeframe, start_pos, count)
        )

    def copy_bars_range(
        self, symbol: str, timeframe: Timeframe, date_from_epoch: int, date_to_epoch: int
    ) -> tuple[Bar, ...]:
        bars = self._inner.copy_bars_range(
            symbol,
            timeframe,
            self._clock.to_wall_seconds(date_from_epoch),
            self._clock.to_wall_seconds(date_to_epoch),
        )
        return tuple(
            dataclasses.replace(
                bar, time=self._clock.to_utc_seconds(bar.time), source_time=bar.time
            )
            for bar in bars
        )

    def copy_ticks(self, symbol: str, date_from_msc: int, count: int) -> tuple[Tick, ...]:
        wall_from = self._clock.to_wall_msc(date_from_msc)
        return tuple(self._tick(tick) for tick in self._inner.copy_ticks(symbol, wall_from, count))

    def order_book(self, symbol: str) -> tuple[BookLevel, ...] | None:
        return self._inner.order_book(symbol)

    def orders(self) -> tuple[Mt5Order, ...]:
        return tuple(self._order(order) for order in self._inner.orders())

    def positions(self) -> tuple[Mt5Position, ...]:
        return tuple(
            dataclasses.replace(position, time_open=self._clock.to_utc_seconds(position.time_open))
            for position in self._inner.positions()
        )

    def history_orders(self, date_from_epoch: int, date_to_epoch: int) -> tuple[Mt5Order, ...]:
        orders = self._inner.history_orders(
            self._clock.to_wall_seconds(date_from_epoch), self._clock.to_wall_seconds(date_to_epoch)
        )
        return tuple(self._order(order) for order in orders)

    def history_deals(self, date_from_epoch: int, date_to_epoch: int) -> tuple[Mt5Deal, ...]:
        deals = self._inner.history_deals(
            self._clock.to_wall_seconds(date_from_epoch), self._clock.to_wall_seconds(date_to_epoch)
        )
        return tuple(
            dataclasses.replace(deal, time_msc=self._clock.to_utc_msc(deal.time_msc))
            for deal in deals
        )

    def order_check(self, intent: NewOrderIntent) -> OrderCheckResult:
        return self._inner.order_check(
            dataclasses.replace(intent, expiration=self._outbound_expiration(intent.expiration))
        )

    def order_send(self, intent: NewOrderIntent) -> OrderSendResult:
        return self._inner.order_send(
            dataclasses.replace(intent, expiration=self._outbound_expiration(intent.expiration))
        )

    def modify_order(self, request: ModifyOrderRequest) -> OrderSendResult:
        return self._inner.modify_order(
            dataclasses.replace(request, expiration=self._outbound_expiration(request.expiration))
        )

    def cancel_order(self, ticket: int) -> OrderSendResult:
        return self._inner.cancel_order(ticket)

    def last_error(self) -> Mt5ErrorInfo:
        return self._inner.last_error()
