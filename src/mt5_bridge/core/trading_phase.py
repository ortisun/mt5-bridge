"""Trading-phase classification from a symbol's own trading_allowed/sessions
data (plan §114 "B3 Specifics": "opening/closing auctions: terminal
visibility of trading phases is limited"; audit F4b).

``classify_trading_phase`` is a pure function over exactly the fields the
terminal already reports through ``symbol_info`` (``trading_allowed`` and
``sessions``, both on ``SymbolInfo``) plus a configured auction-window
duration -- no other signal. It is a bridge-side *approximation*, never a
broker-confirmed exchange phase: qualifying it against B3's own actual
auction schedule requires external, broker-specific confirmation (ADR-078),
which this classification explicitly does not attempt.

``now`` must already be expressed in the same time basis as the declared
``SessionWindow``s (the caller's job, following the same never-infer-a-zone
discipline as ``core.daily_loss_baseline.TradingDayPolicy`` and
``mt5.terminal_time.ServerWallClock`` -- this module never itself converts
a time zone or guesses one from magnitude).
"""

from datetime import datetime, timedelta

from mt5_bridge.mt5.models import SymbolInfo, TradingPhase


def classify_trading_phase(
    symbol: SymbolInfo, *, now: datetime, auction_window: timedelta
) -> TradingPhase:
    if type(auction_window) is not timedelta or auction_window <= timedelta(0):
        raise ValueError("trading-phase classification requires a positive auction window")
    if not symbol.trading_allowed:
        return TradingPhase.HALTED
    window = next((w for w in symbol.sessions if w.weekday == now.weekday()), None)
    if window is None:
        return TradingPhase.CLOSED
    local_time = now.time()
    open_dt = datetime.combine(now.date(), window.open_time, tzinfo=now.tzinfo)
    close_dt = datetime.combine(now.date(), window.close_time, tzinfo=now.tzinfo)
    if open_dt - auction_window <= now < open_dt:
        return TradingPhase.PRE_OPEN_AUCTION
    if close_dt - auction_window <= now < close_dt:
        return TradingPhase.CLOSING_AUCTION
    if window.open_time <= local_time < window.close_time:
        return TradingPhase.CONTINUOUS
    return TradingPhase.CLOSED


__all__ = ["classify_trading_phase"]
