"""Trading-phase classification from a symbol's own trading_allowed/sessions
data (plan §114 "B3 Specifics", audit F4b). This is a bridge-side
approximation from session windows, never a broker-confirmed exchange phase."""

from datetime import UTC, datetime, time, timedelta
from decimal import Decimal

import pytest

from mt5_bridge.core.trading_phase import classify_trading_phase
from mt5_bridge.mt5.models import FillingMode, SessionWindow, SymbolInfo, TimeInForce, TradingPhase

_AUCTION_WINDOW = timedelta(minutes=5)


def _symbol(*, trading_allowed: bool = True, sessions: tuple[SessionWindow, ...]) -> SymbolInfo:
    return SymbolInfo(
        name="PETR4",
        description="Petrobras PN",
        currency_base="BRL",
        currency_profit="BRL",
        currency_margin="BRL",
        digits=2,
        tick_size=Decimal("0.01"),
        tick_value=Decimal("1.00"),
        volume_min=Decimal(100),
        volume_max=Decimal(100000),
        volume_step=Decimal(100),
        contract_size=Decimal(1),
        filling_modes=frozenset({FillingMode.RETURN}),
        time_in_force_modes=frozenset({TimeInForce.DAY}),
        is_continuous_series=False,
        trading_allowed=trading_allowed,
        sessions=sessions,
    )


def _monday_session() -> tuple[SessionWindow, ...]:
    return (SessionWindow(weekday=0, open_time=time(10, 0), close_time=time(17, 0)),)


def _at(hour: int, minute: int) -> datetime:
    # 2026-01-05 is a Monday.
    return datetime(2026, 1, 5, hour, minute, tzinfo=UTC)


def test_within_the_session_window_is_continuous() -> None:
    symbol = _symbol(sessions=_monday_session())
    phase = classify_trading_phase(symbol, now=_at(12, 0), auction_window=_AUCTION_WINDOW)
    assert phase is TradingPhase.CONTINUOUS


def test_shortly_before_open_is_the_pre_open_auction() -> None:
    symbol = _symbol(sessions=_monday_session())
    phase = classify_trading_phase(symbol, now=_at(9, 57), auction_window=_AUCTION_WINDOW)
    assert phase is TradingPhase.PRE_OPEN_AUCTION


def test_at_the_open_boundary_is_continuous_not_auction() -> None:
    symbol = _symbol(sessions=_monday_session())
    phase = classify_trading_phase(symbol, now=_at(10, 0), auction_window=_AUCTION_WINDOW)
    assert phase is TradingPhase.CONTINUOUS


def test_shortly_before_close_is_the_closing_auction() -> None:
    symbol = _symbol(sessions=_monday_session())
    phase = classify_trading_phase(symbol, now=_at(16, 58), auction_window=_AUCTION_WINDOW)
    assert phase is TradingPhase.CLOSING_AUCTION


def test_at_the_close_boundary_is_closed() -> None:
    symbol = _symbol(sessions=_monday_session())
    phase = classify_trading_phase(symbol, now=_at(17, 0), auction_window=_AUCTION_WINDOW)
    assert phase is TradingPhase.CLOSED


def test_well_before_the_pre_open_window_is_closed() -> None:
    symbol = _symbol(sessions=_monday_session())
    phase = classify_trading_phase(symbol, now=_at(8, 0), auction_window=_AUCTION_WINDOW)
    assert phase is TradingPhase.CLOSED


def test_a_weekday_with_no_declared_session_is_closed() -> None:
    symbol = _symbol(sessions=_monday_session())
    # 2026-01-06 is a Tuesday; only Monday has a declared session here.
    phase = classify_trading_phase(
        symbol, now=datetime(2026, 1, 6, 12, 0, tzinfo=UTC), auction_window=_AUCTION_WINDOW
    )
    assert phase is TradingPhase.CLOSED


def test_trading_not_allowed_is_halted_even_inside_the_session() -> None:
    symbol = _symbol(trading_allowed=False, sessions=_monday_session())
    phase = classify_trading_phase(symbol, now=_at(12, 0), auction_window=_AUCTION_WINDOW)
    assert phase is TradingPhase.HALTED


def test_rejects_a_non_positive_auction_window() -> None:
    symbol = _symbol(sessions=_monday_session())
    with pytest.raises(ValueError):
        classify_trading_phase(symbol, now=_at(12, 0), auction_window=timedelta(0))
