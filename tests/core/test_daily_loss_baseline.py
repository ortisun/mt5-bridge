"""DailyLossBaseline/TradingDayPolicy: the persisted trading-day baseline
(audit F1) — same-day restart keeps the loss, rollover resets per policy,
account mismatch fails closed instead of resetting."""

from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import pytest

from mt5_bridge.core.daily_loss_baseline import DailyLossBaseline, TradingDayPolicy
from mt5_bridge.core.database import BridgeDatabase

SAO_PAULO = TradingDayPolicy("America/Sao_Paulo")

# 2024-01-01 12:00 UTC is 2024-01-01 09:00 America/Sao_Paulo (UTC-3) — same
# calendar day in both; used as a stable "midday" instant across tests.
DAY_1_NOON_UTC = datetime(2024, 1, 1, 12, 0, tzinfo=UTC)
# 2024-01-02 01:30 UTC is 2024-01-01 22:30 America/Sao_Paulo — still day 1
# locally, despite being day 2 in UTC: proves the zone, not UTC, decides.
DAY_1_LATE_UTC_STILL_DAY_1_LOCAL = datetime(2024, 1, 2, 1, 30, tzinfo=UTC)
DAY_2_NOON_UTC = datetime(2024, 1, 2, 12, 0, tzinfo=UTC)


class TestTradingDayPolicy:
    def test_an_unknown_zone_is_refused_eagerly(self) -> None:
        with pytest.raises(ValueError, match="time zone"):
            TradingDayPolicy("Mars/Olympus_Mons")

    def test_trading_day_follows_the_configured_zone_not_utc(self) -> None:
        assert SAO_PAULO.trading_day(DAY_1_LATE_UTC_STILL_DAY_1_LOCAL) == "2024-01-01"
        assert SAO_PAULO.trading_day(DAY_2_NOON_UTC) == "2024-01-02"


class TestDailyLossBaselineResolve:
    def test_the_first_ever_boot_establishes_the_baseline_from_current_equity(self) -> None:
        baseline = DailyLossBaseline(BridgeDatabase(":memory:"))

        resolution = baseline.resolve(
            account_id="123",
            trading_day_policy=SAO_PAULO,
            now=DAY_1_NOON_UTC,
            current_equity=Decimal(100_000),
        )

        assert resolution.stale is False
        assert resolution.baseline_equity == Decimal(100_000)

    def test_a_same_day_restart_keeps_the_original_baseline_not_the_new_reading(
        self, tmp_path: Path
    ) -> None:
        db_path = str(tmp_path / "bridge.db")
        first_boot = DailyLossBaseline(BridgeDatabase(db_path))
        first_boot.resolve(
            account_id="123",
            trading_day_policy=SAO_PAULO,
            now=DAY_1_NOON_UTC,
            current_equity=Decimal(100_000),
        )

        # Equity dropped to 90,000 by the time of the restart, same trading day.
        second_boot = DailyLossBaseline(BridgeDatabase(db_path))
        resolution = second_boot.resolve(
            account_id="123",
            trading_day_policy=SAO_PAULO,
            now=DAY_1_LATE_UTC_STILL_DAY_1_LOCAL,
            current_equity=Decimal(90_000),
        )

        assert resolution.stale is False
        assert resolution.baseline_equity == Decimal(100_000)  # not 90,000

    def test_a_new_trading_day_resets_the_baseline_to_the_fresh_reading(
        self, tmp_path: Path
    ) -> None:
        db_path = str(tmp_path / "bridge.db")
        first_boot = DailyLossBaseline(BridgeDatabase(db_path))
        first_boot.resolve(
            account_id="123",
            trading_day_policy=SAO_PAULO,
            now=DAY_1_NOON_UTC,
            current_equity=Decimal(100_000),
        )

        second_boot = DailyLossBaseline(BridgeDatabase(db_path))
        resolution = second_boot.resolve(
            account_id="123",
            trading_day_policy=SAO_PAULO,
            now=DAY_2_NOON_UTC,
            current_equity=Decimal(90_000),
        )

        assert resolution.stale is False
        assert resolution.baseline_equity == Decimal(90_000)

    def test_the_reset_baseline_then_survives_its_own_same_day_restart(
        self, tmp_path: Path
    ) -> None:
        db_path = str(tmp_path / "bridge.db")
        DailyLossBaseline(BridgeDatabase(db_path)).resolve(
            account_id="123",
            trading_day_policy=SAO_PAULO,
            now=DAY_1_NOON_UTC,
            current_equity=Decimal(100_000),
        )
        DailyLossBaseline(BridgeDatabase(db_path)).resolve(
            account_id="123",
            trading_day_policy=SAO_PAULO,
            now=DAY_2_NOON_UTC,
            current_equity=Decimal(90_000),
        )

        third_boot = DailyLossBaseline(BridgeDatabase(db_path))
        resolution = third_boot.resolve(
            account_id="123",
            trading_day_policy=SAO_PAULO,
            now=DAY_2_NOON_UTC,
            current_equity=Decimal(70_000),  # dropped further, still day 2
        )

        assert resolution.stale is False
        assert resolution.baseline_equity == Decimal(90_000)  # day 2's own baseline, not 70,000

    def test_a_different_bound_account_is_rejected_not_overwritten(self, tmp_path: Path) -> None:
        db_path = str(tmp_path / "bridge.db")
        DailyLossBaseline(BridgeDatabase(db_path)).resolve(
            account_id="123",
            trading_day_policy=SAO_PAULO,
            now=DAY_1_NOON_UTC,
            current_equity=Decimal(100_000),
        )

        mismatched = DailyLossBaseline(BridgeDatabase(db_path))
        resolution = mismatched.resolve(
            account_id="999",
            trading_day_policy=SAO_PAULO,
            now=DAY_1_LATE_UTC_STILL_DAY_1_LOCAL,
            current_equity=Decimal(50_000),
        )

        assert resolution.stale is True
        assert resolution.reason is not None
        assert "123" in resolution.reason
        assert "999" in resolution.reason

        # Never overwritten by the mismatched attempt: the original account's
        # baseline is exactly as it was.
        still_bound = DailyLossBaseline(BridgeDatabase(db_path)).resolve(
            account_id="123",
            trading_day_policy=SAO_PAULO,
            now=DAY_1_LATE_UTC_STILL_DAY_1_LOCAL,
            current_equity=Decimal(50_000),
        )
        assert still_bound.stale is False
        assert still_bound.baseline_equity == Decimal(100_000)
