"""Real ``LimitsState`` aggregates, replacing a hardcoded-zero placeholder.

``LimitsGuard`` itself stays pure (plan §114: an independent second line of
defense); this module is where the bridge computes what it actually knows
right now from the journal and a tracked account equity, so the guard's
per-order-volume, orders-per-minute, open-notional and daily-loss rules can
all actually fire.
"""

from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal

from mt5_bridge.clock import Clock
from mt5_bridge.core.limits_guard import LimitsState
from mt5_bridge.core.order_journal import JournalEntry, OrderJournal

_DEFAULT_WINDOW_SECONDS = 60


@dataclass
class AccountEquityTracker:
    """Tracks a session-start equity baseline and today's realized drawdown.

    The baseline is set once, by the first ``record`` call (meant to be the
    account snapshot read at boot, seeded from the persisted
    ``DailyLossBaseline`` — audit F1), and never moves — a later rally does
    not raise the bar for what counts as "loss" (plan §107: max daily loss is
    measured from a fixed reference, not a trailing high-water mark).
    """

    session_start_equity: Decimal | None = None
    current_equity: Decimal | None = None
    last_recorded_at: datetime | None = None
    """Set only when a caller supplies ``now`` — production always does
    (``main.boot()``, ``api.background.snapshot_equity_once``); a caller that
    never does simply opts out of the age-based staleness check in
    ``compute_limits_state`` (see its docstring)."""
    evidence_unavailable_reason: str | None = None
    """Set only by ``mark_unavailable`` (e.g. a persisted daily-loss baseline
    bound to a different account — audit F1). Once set, the evidence stays
    unavailable for the rest of this process's life: an account-identity
    mismatch is a deployment error a human must fix, not something a later
    ``record`` call can resolve at runtime."""

    def record(self, equity: Decimal, *, now: datetime | None = None) -> None:
        if self.session_start_equity is None:
            self.session_start_equity = equity
        self.current_equity = equity
        if now is not None:
            self.last_recorded_at = now

    def mark_unavailable(self, reason: str) -> None:
        self.evidence_unavailable_reason = reason

    def daily_loss(self) -> Decimal:
        if self.session_start_equity is None or self.current_equity is None:
            return Decimal(0)
        return max(Decimal(0), self.session_start_equity - self.current_equity)


def _daily_loss_evidence_is_stale(
    tracker: AccountEquityTracker, *, now: datetime, max_age_seconds: float | None
) -> bool:
    if tracker.evidence_unavailable_reason is not None:
        return True
    if tracker.session_start_equity is None or tracker.current_equity is None:
        return True
    if max_age_seconds is not None and tracker.last_recorded_at is not None:
        age = (now - tracker.last_recorded_at).total_seconds()
        if age > max_age_seconds:
            return True
    return False


def _exposure_price(entry: JournalEntry) -> Decimal | None:
    """The order's own price when known, otherwise the fresh-quote bound
    recorded at submit for an unpriced (MARKET) order (audit F2)."""
    return entry.price if entry.price is not None else entry.notional_price


def compute_limits_state(
    *,
    journal: OrderJournal,
    equity_tracker: AccountEquityTracker,
    clock: Clock,
    window_seconds: int = _DEFAULT_WINDOW_SECONDS,
    equity_max_age_seconds: float | None = None,
) -> LimitsState:
    """``equity_max_age_seconds`` gates the age-based half of the staleness
    check (audit F1's "unavailable equity fails closed"): ``None`` (the
    default) disables only that half, not the other, unconditional one — a
    tracker that never recorded anything at all (``session_start_equity`` or
    ``current_equity`` still ``None``) is always stale, with or without an
    age bound, because there is then no baseline to trust regardless of age.
    """
    now = clock.now_utc()
    since = now - timedelta(seconds=window_seconds)
    orders_in_last_minute = journal.count_created_since_no_transaction(since=since)
    open_notional = sum(
        (
            entry.volume * price
            for entry in journal.list_open_exposure_no_transaction()
            if (price := _exposure_price(entry)) is not None
        ),
        start=Decimal(0),
    )
    return LimitsState(
        open_notional=open_notional,
        daily_loss=equity_tracker.daily_loss(),
        orders_in_last_minute=orders_in_last_minute,
        daily_loss_evidence_stale=_daily_loss_evidence_is_stale(
            equity_tracker, now=now, max_age_seconds=equity_max_age_seconds
        ),
    )
