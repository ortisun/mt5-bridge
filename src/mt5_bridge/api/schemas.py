"""Pydantic request/response models mirroring contracts/mt5_bridge/openapi.yaml.

Prices, volumes and money amounts are transmitted as JSON strings (never a
bare JSON number): ``DecimalStr`` requires a string on the way in and always
serializes back to one, so a client's JSON parser never has a chance to
round a price through ``float`` (plan rule 33).
"""

from decimal import Decimal, InvalidOperation
from typing import Annotated

from pydantic import BaseModel, BeforeValidator, ConfigDict, PlainSerializer

from mt5_bridge.core.oms_state import BridgeOrderState, ReplaceOutcome
from mt5_bridge.mt5.models import (
    FillingMode,
    OrderSide,
    OrderType,
    PositionAccounting,
    TimeInForce,
    TradingPhase,
)


def _parse_decimal(value: object) -> Decimal:
    if isinstance(value, Decimal):
        return value
    if isinstance(value, str):
        try:
            return Decimal(value)
        except InvalidOperation as error:
            raise ValueError(f"{value!r} is not a valid decimal string") from error
    # ValueError, not TypeError: Pydantic reports only a ValueError as a
    # validation fault (422); a TypeError would escape as a 500.
    raise ValueError("expected a decimal-formatted string, not a bare JSON number")


DecimalStr = Annotated[Decimal, BeforeValidator(_parse_decimal), PlainSerializer(str)]


class ApiModel(BaseModel):
    """Lax enough to coerce ``"BUY"`` into ``OrderSide.BUY``; ``DecimalStr``
    fields carry their own strictness (a bare JSON number is always
    rejected) independent of this setting."""

    model_config = ConfigDict()


class HealthResponse(ApiModel):
    status: str
    terminal_connected: bool
    algo_trading_enabled: bool
    kill_switch_state: str
    clock_drift_seconds: float
    clock_drift_within_threshold: bool
    auction_participation_supported: bool
    tick_poll_latency_seconds: float | None = None
    bar_poll_latency_seconds: float | None = None
    execution_poll_latency_seconds: float | None = None


class TerminalResponse(ApiModel):
    connected: bool
    algo_trading_enabled: bool
    build: int
    name: str
    company: str


class AccountResponse(ApiModel):
    login: int
    balance: DecimalStr
    equity: DecimalStr
    margin: DecimalStr
    margin_free: DecimalStr
    currency: str
    position_accounting: PositionAccounting
    trade_allowed: bool


class SymbolSummary(ApiModel):
    name: str
    description: str
    is_continuous_series: bool
    trading_allowed: bool


class SymbolDetail(SymbolSummary):
    tick_size: DecimalStr
    volume_min: DecimalStr
    volume_max: DecimalStr
    volume_step: DecimalStr
    filling_modes: list[FillingMode]
    time_in_force_modes: list[TimeInForce]
    trading_phase: TradingPhase


class BarResponse(ApiModel):
    symbol: str
    timeframe: str
    time: int
    open: DecimalStr
    high: DecimalStr
    low: DecimalStr
    close: DecimalStr
    volume: DecimalStr


class TickResponse(ApiModel):
    symbol: str
    time_msc: int
    bid: DecimalStr
    ask: DecimalStr
    last: DecimalStr
    volume: DecimalStr


class BarsPageResponse(ApiModel):
    symbol: str
    timeframe: str
    from_epoch: int
    to_epoch: int
    page: int
    total_pages: int
    next_page: int | None
    bars: list[BarResponse]


class TicksPageResponse(ApiModel):
    symbol: str
    from_time_msc: int
    to_time_msc: int
    next_cursor: str | None
    ticks: list[TickResponse]


class OrderResponse(ApiModel):
    client_order_id: str
    symbol: str
    side: OrderSide
    order_type: OrderType
    volume: DecimalStr
    price: DecimalStr | None
    time_in_force: TimeInForce
    state: BridgeOrderState
    ticket: int | None
    mt5_retcode: int | None
    mt5_last_error: str | None
    reason: str | None


class PositionResponse(ApiModel):
    symbol: str
    side: OrderSide
    volume: DecimalStr
    price_open: DecimalStr
    price_current: DecimalStr


class SubmitOrderRequest(ApiModel):
    client_order_id: str
    symbol: str
    side: OrderSide
    type: OrderType
    volume: DecimalStr
    time_in_force: TimeInForce
    price: DecimalStr | None = None
    stop_loss: DecimalStr | None = None
    take_profit: DecimalStr | None = None
    expiration: int | None = None


class AmendOrderRequest(ApiModel):
    price: DecimalStr | None = None
    stop_loss: DecimalStr | None = None
    take_profit: DecimalStr | None = None
    expiration: int | None = None


class ReplaceOrderRequest(ApiModel):
    operation_id: str
    new_client_order_id: str
    symbol: str
    side: OrderSide
    type: OrderType
    volume: DecimalStr
    time_in_force: TimeInForce
    price: DecimalStr | None = None
    stop_loss: DecimalStr | None = None
    take_profit: DecimalStr | None = None
    expiration: int | None = None


class ReplaceOrderResponse(ApiModel):
    operation_id: str
    outcome: ReplaceOutcome
    original: OrderResponse | None
    new: OrderResponse | None


class KillSwitchRequest(ApiModel):
    action: str


class KillSwitchResponse(ApiModel):
    state: str
    reason: str | None
    updated_at: str


class OrderCheckResponse(ApiModel):
    retcode: int
    comment: str
    balance: DecimalStr
    equity: DecimalStr
    margin: DecimalStr
    margin_free: DecimalStr


class HistoryOrderResponse(ApiModel):
    """The terminal's own record, not the bridge journal's (no client_order_id
    or bridge state — that is what ``GET /v1/orders/{client_order_id}`` is
    for)."""

    ticket: int
    symbol: str
    side: OrderSide
    order_type: OrderType
    volume_initial: DecimalStr
    volume_current: DecimalStr
    price_open: DecimalStr
    time_setup: int
    time_in_force: TimeInForce


class DealResponse(ApiModel):
    ticket: int
    order_ticket: int
    symbol: str
    side: OrderSide
    volume: DecimalStr
    price: DecimalStr
    commission: DecimalStr
    swap: DecimalStr
    profit: DecimalStr
    time_msc: int


class BookLevelResponse(ApiModel):
    side: str
    price: DecimalStr
    volume: DecimalStr
