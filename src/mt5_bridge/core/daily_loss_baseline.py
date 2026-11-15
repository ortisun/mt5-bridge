"""Persists the trading-day daily-loss baseline across restarts (audit F1).

``AccountEquityTracker`` (``core/limits_state_provider.py``) only ever held
``session_start_equity`` in memory, and ``main.boot()`` reseeded it from
whatever the account's equity happened to be at that exact moment — so a
restart silently forgave the day's consumed loss capacity (plan §107's "max
daily loss", §114's independent ``LimitsGuard``). This module is the durable
side of that baseline: one row in the bridge's own SQLite file (the same
store and transaction discipline as ``OrderJournal``/``KillSwitch``), bound
to a trading day and an account.

``TradingDayPolicy`` defines the rollover boundary explicitly — an IANA zone
whose local midnight starts a new trading day — following the same
never-infer-it convention as ``mt5.terminal_time.ServerWallClock`` (a
proven, versioned, configured zone; never guessed from magnitude or the
current clock). The bridge trades B3 only, so the default is B3's own
trading day, not UTC's.
"""

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from mt5_bridge.core.database import BridgeDatabase


class TradingDayPolicy:
    """Maps an instant to its trading-day key under one configured IANA zone."""

    def __init__(self, zone_name: str) -> None:
        try:
            self._zone = ZoneInfo(zone_name)
        except (ZoneInfoNotFoundError, ValueError) as error:
            raise ValueError(f"unknown trading-day time zone {zone_name!r}") from error
        self.zone_name = zone_name

    def trading_day(self, instant: datetime) -> str:
        return instant.astimezone(self._zone).date().isoformat()

    def localize(self, instant: datetime) -> datetime:
        """``instant`` expressed in this policy's configured zone -- the
        same conversion ``trading_day`` uses, exposed for any other reader
        (e.g. ``core.trading_phase.classify_trading_phase``) that needs a
        session-local instant rather than just its date."""
        return instant.astimezone(self._zone)


@dataclass(frozen=True, slots=True)
class BaselineResolution:
    baseline_equity: Decimal
    """The baseline to seed into ``AccountEquityTracker`` — meaningless when
    ``stale`` is true (never construct a baseline from a foreign account)."""
    stale: bool
    reason: str | None


class DailyLossBaseline:
    """One VM-local, account-bound row (plan §114: the bridge binds to one
    account for its whole process lifetime — the same single-row convention
    as ``KillSwitch``'s ``kill_switch_state``)."""

    def __init__(self, db: BridgeDatabase) -> None:
        self._db = db

    def resolve(
        self,
        *,
        account_id: str,
        trading_day_policy: TradingDayPolicy,
        now: datetime,
        current_equity: Decimal,
    ) -> BaselineResolution:
        today = trading_day_policy.trading_day(now)
        row = self._db.fetchone(
            "SELECT account_id, trading_day, baseline_equity FROM daily_loss_baseline WHERE id = 1"
        )
        if row is None:
            self._store(
                account_id=account_id, trading_day=today, baseline_equity=current_equity, now=now
            )
            return BaselineResolution(baseline_equity=current_equity, stale=False, reason=None)

        stored_account_id = str(row["account_id"])
        if stored_account_id != account_id:
            # Never trusted, never overwritten: a foreign baseline is neither
            # a valid bound nor evidence to silently replace with today's
            # reading (plan rule 43: never silently reset consumed capacity).
            return BaselineResolution(
                baseline_equity=Decimal(0),
                stale=True,
                reason=(
                    f"persisted daily-loss baseline belongs to account "
                    f"{stored_account_id!r}, not the bound account {account_id!r}"
                ),
            )

        if str(row["trading_day"]) != today:
            self._store(
                account_id=account_id, trading_day=today, baseline_equity=current_equity, now=now
            )
            return BaselineResolution(baseline_equity=current_equity, stale=False, reason=None)

        return BaselineResolution(
            baseline_equity=Decimal(str(row["baseline_equity"])), stale=False, reason=None
        )

    def _store(
        self, *, account_id: str, trading_day: str, baseline_equity: Decimal, now: datetime
    ) -> None:
        with self._db.transaction() as conn:
            conn.execute(
                "INSERT INTO daily_loss_baseline (id, account_id, trading_day, baseline_equity, updated_at) "
                "VALUES (1, ?, ?, ?, ?) "
                "ON CONFLICT(id) DO UPDATE SET account_id = excluded.account_id, "
                "trading_day = excluded.trading_day, baseline_equity = excluded.baseline_equity, "
                "updated_at = excluded.updated_at",
                (account_id, trading_day, str(baseline_equity), now.isoformat()),
            )
