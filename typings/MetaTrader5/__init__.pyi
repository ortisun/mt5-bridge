"""Minimal, hand-written, UNVALIDATED type stub for the ``MetaTrader5`` package.

Covers only the subset of the real Windows-only API that
``mt5_bridge.mt5.real_client`` calls (per
https://www.mql5.com/en/docs/python_metatrader5, reviewed 2026-09-20 per
plan §116, against package version 5.0.6180 -- the exact pin in
``pyproject.toml``/``uv.lock``, published 2026-09-05;
ADR-025's W13b amendment). Re-review this stub and bump this note whenever
that pin changes. It exists so ``real_client.py`` can type-check under Pyright
strict on macOS/Linux, where the real package cannot be installed (it ships
only Windows wheels). Field lists on the return "info" types are trimmed to
what the bridge actually reads, not the full real structure (which has many
more fields); ``copy_rates_from_pos``/``copy_ticks_from`` return numpy
structured arrays at runtime, modeled here as a plain sequence of
subscriptable rows since real_client.py never uses attribute access on them
(bracket access on each row works identically against the real numpy
result). None of this has been executed against a real terminal.
"""

from collections.abc import Sequence
from typing import NamedTuple

class AccountInfo(NamedTuple):
    login: int
    balance: float
    equity: float
    margin: float
    margin_free: float
    currency: str
    margin_mode: int
    trade_allowed: bool
    server: str
    trade_mode: int

class TerminalInfo(NamedTuple):
    connected: bool
    trade_allowed: bool
    build: int
    name: str
    company: str

class SymbolInfo(NamedTuple):
    name: str
    description: str
    currency_base: str
    currency_profit: str
    currency_margin: str
    digits: int
    trade_tick_size: float
    trade_tick_value: float
    volume_min: float
    volume_max: float
    volume_step: float
    trade_contract_size: float
    filling_mode: int
    expiration_mode: int
    trade_mode: int
    visible: bool

class Tick(NamedTuple):
    time: int
    time_msc: int
    bid: float
    ask: float
    last: float
    volume: float

class TradeOrder(NamedTuple):
    ticket: int
    symbol: str
    type: int
    state: int
    volume_initial: float
    volume_current: float
    price_open: float
    sl: float
    tp: float
    time_setup: int
    time_expiration: int
    magic: int
    comment: str
    type_time: int
    type_filling: int

class TradePosition(NamedTuple):
    ticket: int
    symbol: str
    type: int
    volume: float
    price_open: float
    price_current: float
    swap: float
    profit: float
    time: int
    magic: int
    comment: str

class TradeDeal(NamedTuple):
    ticket: int
    order: int
    symbol: str
    type: int
    entry: int
    volume: float
    price: float
    commission: float
    swap: float
    profit: float
    time_msc: int
    magic: int
    comment: str

class OrderCheckResult(NamedTuple):
    retcode: int
    comment: str
    balance: float
    equity: float
    margin: float
    margin_free: float

class OrderSendResult(NamedTuple):
    retcode: int
    order: int
    deal: int
    volume: float
    price: float
    comment: str

class BookInfo(NamedTuple):
    type: int
    price: float
    volume: int
    volume_dbl: float

# A row of a real numpy structured array supports both bracket access by
# field name and by position; real_client.py only ever does the former.
BarRow = Sequence[object]

def initialize(
    path: str | None = None,
    *,
    login: int | None = None,
    password: str | None = None,
    server: str | None = None,
    timeout: int | None = None,
    portable: bool = False,
) -> bool: ...
def shutdown() -> None: ...
def last_error() -> tuple[int, str]: ...
def account_info() -> AccountInfo | None: ...
def terminal_info() -> TerminalInfo | None: ...
def symbols_get(group: str | None = None) -> tuple[SymbolInfo, ...] | None: ...
def symbol_info(symbol: str) -> SymbolInfo | None: ...
def symbol_info_tick(symbol: str) -> Tick | None: ...
def copy_rates_from_pos(
    symbol: str, timeframe: int, start_pos: int, count: int
) -> Sequence[BarRow] | None: ...
def copy_ticks_from(
    symbol: str, date_from: int, count: int, flags: int
) -> Sequence[BarRow] | None: ...
def orders_get(
    symbol: str | None = None, group: str | None = None
) -> tuple[TradeOrder, ...] | None: ...
def positions_get(
    symbol: str | None = None, group: str | None = None
) -> tuple[TradePosition, ...] | None: ...
def history_orders_get(
    date_from: int, date_to: int, group: str | None = None
) -> tuple[TradeOrder, ...] | None: ...
def history_deals_get(
    date_from: int, date_to: int, group: str | None = None
) -> tuple[TradeDeal, ...] | None: ...
def market_book_add(symbol: str) -> bool: ...
def market_book_get(symbol: str) -> tuple[BookInfo, ...] | None: ...
def order_check(request: dict[str, object]) -> OrderCheckResult | None: ...
def order_send(request: dict[str, object]) -> OrderSendResult | None: ...

TIMEFRAME_M1: int
TIMEFRAME_M5: int
TIMEFRAME_M15: int
TIMEFRAME_M30: int
TIMEFRAME_H1: int
TIMEFRAME_H4: int
TIMEFRAME_D1: int
TIMEFRAME_W1: int
TIMEFRAME_MN1: int

BOOK_TYPE_SELL: int
BOOK_TYPE_BUY: int
BOOK_TYPE_SELL_MARKET: int
BOOK_TYPE_BUY_MARKET: int

ORDER_TYPE_BUY: int
ORDER_TYPE_SELL: int
ORDER_TYPE_BUY_LIMIT: int
ORDER_TYPE_SELL_LIMIT: int
ORDER_TYPE_BUY_STOP: int
ORDER_TYPE_SELL_STOP: int

# POSITION_TYPE_*/DEAL_TYPE_* are distinct, smaller enums from ORDER_TYPE_*
# (a position or a deal's *direction* is only ever long or short, though a
# deal's full DEAL_TYPE_* range also covers non-trade entries such as
# balance operations, not modeled here); real_client.py never assumes their
# numeric values coincide with ORDER_TYPE_*, even though MQL5 convention
# makes BUY=0/SELL=1 for all three.
POSITION_TYPE_BUY: int
POSITION_TYPE_SELL: int
DEAL_TYPE_BUY: int
DEAL_TYPE_SELL: int

ORDER_STATE_STARTED: int
ORDER_STATE_PLACED: int
ORDER_STATE_CANCELED: int
ORDER_STATE_PARTIAL: int
ORDER_STATE_FILLED: int
ORDER_STATE_REJECTED: int
ORDER_STATE_EXPIRED: int
ORDER_STATE_REQUEST_ADD: int
ORDER_STATE_REQUEST_MODIFY: int
ORDER_STATE_REQUEST_CANCEL: int

ORDER_FILLING_FOK: int
ORDER_FILLING_IOC: int
ORDER_FILLING_RETURN: int

ORDER_TIME_DAY: int
ORDER_TIME_GTC: int
ORDER_TIME_SPECIFIED: int

TRADE_ACTION_DEAL: int
TRADE_ACTION_PENDING: int
TRADE_ACTION_MODIFY: int
TRADE_ACTION_REMOVE: int

DEAL_ENTRY_IN: int
DEAL_ENTRY_OUT: int
DEAL_ENTRY_INOUT: int
DEAL_ENTRY_OUT_BY: int

ACCOUNT_TRADE_MODE_DEMO: int
ACCOUNT_TRADE_MODE_CONTEST: int
ACCOUNT_TRADE_MODE_REAL: int
ACCOUNT_MARGIN_MODE_RETAIL_NETTING: int
ACCOUNT_MARGIN_MODE_RETAIL_HEDGING: int
ACCOUNT_MARGIN_MODE_EXCHANGE: int
