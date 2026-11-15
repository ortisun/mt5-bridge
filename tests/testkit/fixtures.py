"""Representative B3-like terminal fixtures shared across the bridge tests.

Values are illustrative (chosen for readable arithmetic in assertions), not a
claim of any specific broker's real current contract specification.
"""

from decimal import Decimal

from mt5_bridge.mt5.models import (
    AccountInfo,
    AccountTradeMode,
    FillingMode,
    PositionAccounting,
    SessionWindow,
    SymbolInfo,
    TimeInForce,
)

WINZ25 = "WINZ25"
"""Dated mini-index futures contract — the only WIN symbol orders may use."""

WIN_CONTINUOUS = "WIN$"
"""Broker synthetic continuous series — orders against it must be rejected."""

PETR4 = "PETR4"
"""Standard-lot equity."""

PETR4F = "PETR4F"
"""Odd-lot equity — a distinct symbol from PETR4, never the same Listing."""


def _b3_equity_sessions() -> tuple[SessionWindow, ...]:
    from datetime import time

    return tuple(
        SessionWindow(weekday=weekday, open_time=time(10, 0), close_time=time(17, 0))
        for weekday in range(5)
    )


def dated_future_symbol(name: str = WINZ25) -> SymbolInfo:
    return SymbolInfo(
        name=name,
        description="Mini index futures, dated contract",
        currency_base="BRL",
        currency_profit="BRL",
        currency_margin="BRL",
        digits=0,
        tick_size=Decimal(5),
        tick_value=Decimal("1.00"),
        volume_min=Decimal(1),
        volume_max=Decimal(100),
        volume_step=Decimal(1),
        contract_size=Decimal(1),
        filling_modes=frozenset({FillingMode.RETURN}),
        time_in_force_modes=frozenset({TimeInForce.DAY}),
        is_continuous_series=False,
        trading_allowed=True,
        sessions=_b3_equity_sessions(),
    )


def continuous_future_symbol(name: str = WIN_CONTINUOUS) -> SymbolInfo:
    base = dated_future_symbol(name)
    return SymbolInfo(
        name=base.name,
        description="Mini index futures, broker synthetic continuous series",
        currency_base=base.currency_base,
        currency_profit=base.currency_profit,
        currency_margin=base.currency_margin,
        digits=base.digits,
        tick_size=base.tick_size,
        tick_value=base.tick_value,
        volume_min=base.volume_min,
        volume_max=base.volume_max,
        volume_step=base.volume_step,
        contract_size=base.contract_size,
        filling_modes=base.filling_modes,
        time_in_force_modes=base.time_in_force_modes,
        is_continuous_series=True,
        trading_allowed=base.trading_allowed,
        sessions=base.sessions,
    )


def equity_symbol(name: str = PETR4, *, odd_lot: bool = False) -> SymbolInfo:
    return SymbolInfo(
        name=name,
        description="Standard-lot equity" if not odd_lot else "Odd-lot equity",
        currency_base="BRL",
        currency_profit="BRL",
        currency_margin="BRL",
        digits=2,
        tick_size=Decimal("0.01"),
        tick_value=Decimal("0.01"),
        volume_min=Decimal(1 if odd_lot else 100),
        volume_max=Decimal(10_000),
        volume_step=Decimal(1 if odd_lot else 100),
        contract_size=Decimal(1),
        filling_modes=frozenset({FillingMode.RETURN, FillingMode.IOC}),
        time_in_force_modes=frozenset({TimeInForce.DAY, TimeInForce.GTC}),
        is_continuous_series=False,
        trading_allowed=True,
        sessions=_b3_equity_sessions(),
    )


def default_symbols() -> tuple[SymbolInfo, ...]:
    return (
        dated_future_symbol(),
        continuous_future_symbol(),
        equity_symbol(PETR4),
        equity_symbol(PETR4F, odd_lot=True),
    )


def default_account(
    login: int = 12345678, trade_mode: AccountTradeMode = AccountTradeMode.DEMO
) -> AccountInfo:
    return AccountInfo(
        login=login,
        balance=Decimal("100000.00"),
        equity=Decimal("100000.00"),
        margin=Decimal("0.00"),
        margin_free=Decimal("100000.00"),
        currency="BRL",
        position_accounting=PositionAccounting.NETTING,
        trade_allowed=True,
        server="Demo",
        trade_mode=trade_mode,
    )
