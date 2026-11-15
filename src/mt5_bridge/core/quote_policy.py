"""Conservative price bound for unpriced MARKET orders (audit F2).

A MARKET order carries no price, so ``OrderService`` used to pass
``estimated_notional=0`` to ``LimitsGuard`` — the one order type most likely
to fill immediately could not contribute to the max-open-notional check at
all. ``conservative_notional_price`` derives a bound from the freshest quote
the bridge has: the side that a fill would actually cost (current ask for
BUY, current bid for SELL), in the symbol's own account-currency units, and
refuses to bound the order — never guesses — when no sufficiently fresh
quote exists.
"""

from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal

from mt5_bridge.mt5.models import OrderSide, Tick


@dataclass(frozen=True, slots=True)
class QuoteBoundResult:
    price: Decimal | None
    """The conservative bound, or ``None`` when the order must be rejected
    instead — see ``rejection_reason`` for why."""
    rejection_reason: str | None


def conservative_notional_price(
    tick: Tick | None, *, side: OrderSide, now: datetime, max_age_seconds: float
) -> QuoteBoundResult:
    if tick is None:
        return QuoteBoundResult(
            price=None, rejection_reason="no quote available to bound the order's notional"
        )
    tick_time = datetime.fromtimestamp(tick.time_msc / 1000, tz=UTC)
    age_seconds = (now - tick_time).total_seconds()
    if age_seconds < 0:
        return QuoteBoundResult(
            price=None,
            rejection_reason=f"quote for {tick.symbol} is future-dated",
        )
    if age_seconds > max_age_seconds:
        return QuoteBoundResult(
            price=None,
            rejection_reason=(
                f"quote for {tick.symbol} is {age_seconds:.1f}s old, exceeding the "
                f"{max_age_seconds:.1f}s maximum quote age"
            ),
        )
    price = tick.ask if side is OrderSide.BUY else tick.bid
    if price <= 0:
        side_field = "ask" if side is OrderSide.BUY else "bid"
        return QuoteBoundResult(
            price=None,
            rejection_reason=f"quote for {tick.symbol} has a non-positive {side_field}",
        )
    return QuoteBoundResult(price=price, rejection_reason=None)
