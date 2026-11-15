"""conservative_notional_price: the fresh-quote bound for unpriced MARKET
orders (audit F2) — current ask for BUY, current bid for SELL, rejecting
instead of guessing when no sufficiently fresh quote exists."""

from datetime import UTC, datetime
from decimal import Decimal

from mt5_bridge.core.quote_policy import conservative_notional_price
from mt5_bridge.mt5.models import OrderSide, Tick

NOW = datetime(2024, 1, 1, 12, 0, 0, tzinfo=UTC)
NOW_MSC = int(NOW.timestamp() * 1000)


def tick(
    *, age_seconds: float = 0.0, bid: Decimal = Decimal(100), ask: Decimal = Decimal(101)
) -> Tick:
    return Tick(
        symbol="WINZ25",
        time_msc=NOW_MSC - int(age_seconds * 1000),
        bid=bid,
        ask=ask,
        last=bid,
        volume=Decimal(1),
    )


def test_a_buy_uses_the_ask() -> None:
    result = conservative_notional_price(tick(), side=OrderSide.BUY, now=NOW, max_age_seconds=5.0)

    assert result.price == Decimal(101)
    assert result.rejection_reason is None


def test_a_sell_uses_the_bid() -> None:
    result = conservative_notional_price(tick(), side=OrderSide.SELL, now=NOW, max_age_seconds=5.0)

    assert result.price == Decimal(100)
    assert result.rejection_reason is None


def test_no_quote_is_rejected() -> None:
    result = conservative_notional_price(None, side=OrderSide.BUY, now=NOW, max_age_seconds=5.0)

    assert result.price is None
    assert result.rejection_reason is not None
    assert "no quote" in result.rejection_reason


def test_a_quote_within_the_max_age_is_accepted() -> None:
    result = conservative_notional_price(
        tick(age_seconds=4.9), side=OrderSide.BUY, now=NOW, max_age_seconds=5.0
    )

    assert result.price == Decimal(101)


def test_a_quote_older_than_the_max_age_is_rejected() -> None:
    result = conservative_notional_price(
        tick(age_seconds=5.1), side=OrderSide.BUY, now=NOW, max_age_seconds=5.0
    )

    assert result.price is None
    assert result.rejection_reason is not None
    assert "old" in result.rejection_reason


def test_a_quote_after_the_decision_instant_is_rejected() -> None:
    result = conservative_notional_price(
        tick(age_seconds=-0.001), side=OrderSide.BUY, now=NOW, max_age_seconds=5.0
    )

    assert result.price is None
    assert result.rejection_reason is not None
    assert "future" in result.rejection_reason


def test_a_non_positive_ask_is_rejected() -> None:
    result = conservative_notional_price(
        tick(ask=Decimal(0)), side=OrderSide.BUY, now=NOW, max_age_seconds=5.0
    )

    assert result.price is None
    assert result.rejection_reason is not None
    assert "ask" in result.rejection_reason


def test_a_non_positive_bid_is_rejected() -> None:
    result = conservative_notional_price(
        tick(bid=Decimal(0)), side=OrderSide.SELL, now=NOW, max_age_seconds=5.0
    )

    assert result.price is None
    assert result.rejection_reason is not None
    assert "bid" in result.rejection_reason
