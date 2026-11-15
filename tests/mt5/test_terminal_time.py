"""Server-wall-clock terminals: one conversion to true UTC at the client boundary.

Evidence (M8/069, Rico-DEMO, build 6182): B3's first M1 bar of 2026-09-25
carries the epoch of 10:03 "UTC", but the session opens at 10:00 in
America/Sao_Paulo (13:00 UTC). The terminal reports its server's wall clock
as if it were UTC, so every raw epoch must be shifted exactly once.
"""

from datetime import UTC, datetime
from decimal import Decimal

import pytest

from mt5_bridge.mt5.fake_client import FakeMt5Client
from mt5_bridge.mt5.models import Mt5Credentials, Timeframe
from mt5_bridge.mt5.terminal_time import ServerWallClock, ServerWallClockTerminal

from testkit.clock import FixedClock
from testkit.fixtures import WINZ25, default_account, default_symbols

SAO_PAULO = ServerWallClock("America/Sao_Paulo")
# 2026-09-25 10:03 on the server's wall clock, reported as an epoch.
WALL_1003 = int(datetime(2026, 9, 25, 10, 3, tzinfo=UTC).timestamp())
UTC_1303 = int(datetime(2026, 9, 25, 13, 3, tzinfo=UTC).timestamp())


def wall_terminal() -> tuple[FakeMt5Client, ServerWallClockTerminal]:
    fake = FakeMt5Client(
        symbols=default_symbols(), account=default_account(), clock=FixedClock(WALL_1003 * 1000)
    )
    fake.connect(Mt5Credentials(login=1, password="x", server="Demo"))
    return fake, ServerWallClockTerminal(fake, SAO_PAULO)


def test_the_observed_session_open_maps_to_the_true_utc_instant() -> None:
    assert SAO_PAULO.to_utc_seconds(WALL_1003) == UTC_1303
    assert SAO_PAULO.to_wall_seconds(UTC_1303) == WALL_1003


def test_milliseconds_keep_their_sub_second_part() -> None:
    assert SAO_PAULO.to_utc_msc(WALL_1003 * 1000 + 701) == UTC_1303 * 1000 + 701


def test_an_unknown_zone_is_refused() -> None:
    with pytest.raises(ValueError, match="time zone"):
        ServerWallClock("Mars/Olympus_Mons")


def test_ticks_and_bars_leave_the_boundary_in_true_utc_exactly_once() -> None:
    fake, terminal = wall_terminal()
    fake.set_tick(WINZ25, bid=Decimal(1), ask=Decimal(2), last=Decimal(1), volume=Decimal(1))
    fake.push_bar(WINZ25, Timeframe.M1, time=WALL_1003, o=1, h=1, low=1, c=1, volume=1)

    tick = terminal.symbol_tick(WINZ25)
    bars = terminal.copy_bars(WINZ25, Timeframe.M1, 0, 1)

    assert tick is not None
    assert tick.time_msc == UTC_1303 * 1000
    assert [bar.time for bar in bars] == [UTC_1303]
    assert fake.copy_bars(WINZ25, Timeframe.M1, 0, 1)[0].time == WALL_1003


def test_history_queries_travel_to_the_terminal_on_its_wall_clock() -> None:
    fake, terminal = wall_terminal()
    fake.set_tick(WINZ25, bid=Decimal(1), ask=Decimal(2), last=Decimal(1), volume=Decimal(1))

    ticks = terminal.copy_ticks(WINZ25, UTC_1303 * 1000, 10)
    missed = terminal.copy_ticks(WINZ25, (UTC_1303 + 1) * 1000, 10)

    assert [tick.time_msc for tick in ticks] == [UTC_1303 * 1000]
    assert missed == ()


def test_everything_else_is_delegated_unchanged() -> None:
    fake, terminal = wall_terminal()

    assert terminal.account_info() == fake.account_info()
    assert terminal.symbol_info(WINZ25) == fake.symbol_info(WINZ25)


def test_a_wrapped_terminal_keeps_the_original_epoch_in_the_raw_field() -> None:
    fake, terminal = wall_terminal()
    fake.set_tick(WINZ25, bid=Decimal(1), ask=Decimal(2), last=Decimal(1), volume=Decimal(1))
    fake.push_bar(WINZ25, Timeframe.M1, time=WALL_1003, o=1, h=1, low=1, c=1, volume=1)

    tick = terminal.symbol_tick(WINZ25)
    bar = terminal.copy_bars(WINZ25, Timeframe.M1, 0, 1)[0]

    assert tick is not None
    assert tick.time_msc == UTC_1303 * 1000  # true UTC, converted exactly once
    assert tick.source_time_msc == WALL_1003 * 1000  # the terminal's own, unconverted epoch
    assert bar.time == UTC_1303
    assert bar.source_time == WALL_1003


def test_an_unwrapped_terminal_never_sets_the_raw_field() -> None:
    fake, _terminal = wall_terminal()
    fake.set_tick(WINZ25, bid=Decimal(1), ask=Decimal(2), last=Decimal(1), volume=Decimal(1))
    fake.push_bar(WINZ25, Timeframe.M1, time=WALL_1003, o=1, h=1, low=1, c=1, volume=1)

    tick = fake.symbol_tick(WINZ25)
    bar = fake.copy_bars(WINZ25, Timeframe.M1, 0, 1)[0]

    assert tick is not None
    assert tick.source_time_msc is None
    assert bar.source_time is None
