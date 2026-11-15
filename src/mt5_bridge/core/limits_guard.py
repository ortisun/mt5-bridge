"""The bridge's own last line of defense — independent of any client's risk gate.

Plan §114: "independent LimitsGuard, separate from the client's risk gate:
symbol allowlist, max volume per order, orders per minute, max open
notional, and daily loss based on account equity. A second line of defense
configured in the VM." Pure and synchronous: it takes a snapshot of current
activity and decides; it does not observe the account itself.
"""

from dataclasses import dataclass
from decimal import Decimal


@dataclass(frozen=True, slots=True)
class LimitsConfig:
    symbol_allowlist: frozenset[str] | None
    """``None`` permits any symbol the terminal exposes; otherwise a closed set."""
    max_volume_per_order: Decimal
    max_orders_per_minute: int
    max_open_notional: Decimal
    max_daily_loss: Decimal


@dataclass(frozen=True, slots=True)
class LimitsState:
    """What the guard needs to know about current activity to decide."""

    open_notional: Decimal
    daily_loss: Decimal
    orders_in_last_minute: int
    daily_loss_evidence_stale: bool = False
    """True when the daily-loss figure above cannot be trusted — missing,
    stale past its configured maximum age, or bound to a different account
    (audit F1) — in which case ``check`` fails closed regardless of the
    numeric ``daily_loss`` value, unless the order is ``reduce_only``
    (plan §107: hard limits and the kill switch must never block emergency
    reduction)."""


@dataclass(frozen=True, slots=True)
class LimitsDecision:
    approved: bool
    reasons: tuple[str, ...]


class LimitsGuard:
    def __init__(self, config: LimitsConfig) -> None:
        self._config = config

    def check(
        self,
        *,
        symbol: str,
        volume: Decimal,
        estimated_notional: Decimal,
        state: LimitsState,
        reduce_only: bool = False,
    ) -> LimitsDecision:
        """``reduce_only`` is set only for an emergency-reduction
        order (``OrderService.flatten()``'s closing orders — plan §107's
        ``EmergencyReductionPolicy``, never reachable from a normal submission):
        such an order cannot increase open notional or daily loss by
        construction (capped at the confirmed position, never opening or
        reversing exposure), so the exposure/loss/evidence rules below do not
        apply to it — "integrity, quantity, and participation limits still
        apply" (plan §107), which is why the allowlist, per-order volume and
        per-minute rate checks above run unconditionally.
        """
        reasons: list[str] = []
        allowlist = self._config.symbol_allowlist
        if allowlist is not None and symbol not in allowlist:
            reasons.append(f"symbol {symbol!r} is not in the LimitsGuard allowlist")
        if volume > self._config.max_volume_per_order:
            reasons.append("volume exceeds max_volume_per_order")
        if state.orders_in_last_minute >= self._config.max_orders_per_minute:
            reasons.append("max_orders_per_minute already reached")
        if not reduce_only:
            if state.open_notional + estimated_notional > self._config.max_open_notional:
                reasons.append("order would exceed max_open_notional")
            if state.daily_loss >= self._config.max_daily_loss:
                reasons.append("max_daily_loss already reached")
            if state.daily_loss_evidence_stale:
                reasons.append(
                    "daily-loss evidence is missing, stale or account-mismatched; failing closed"
                )
        return LimitsDecision(approved=not reasons, reasons=tuple(reasons))
