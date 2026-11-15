"""Date ranges read in pages: the page count of bars follows from the dates,
ticks follow a cursor, and both return every item exactly once."""

from decimal import Decimal

import pytest

from mt5_bridge.core.history_paging import (
    PAGE_SIZE,
    InvalidRangeError,
    TickCursor,
    plan_bar_pages,
    read_bar_page,
    read_tick_page,
    start_tick_page,
)
from mt5_bridge.mt5.fake_client import FakeMt5Client
from mt5_bridge.mt5.models import Mt5Credentials, Tick, Timeframe

from testkit.clock import FixedClock
from testkit.fixtures import WINZ25, default_account, default_symbols

NOW_MS = 1_700_000_000_000
NOW = NOW_MS // 1000
MINUTE = 60
DAY = 24 * 60 * MINUTE


class WholeSecondTerminal(FakeMt5Client):
    """Takes the start of a tick request in whole seconds, as the terminal does."""

    def copy_ticks(self, symbol: str, date_from_msc: int, count: int) -> tuple[Tick, ...]:
        return super().copy_ticks(symbol, date_from_msc // 1000 * 1000, count)


def connected_client(clock: FixedClock) -> WholeSecondTerminal:
    client = WholeSecondTerminal(symbols=default_symbols(), account=default_account(), clock=clock)
    client.connect(Mt5Credentials(login=1, password="x", server="Demo"))
    return client


def push_bars(client: FakeMt5Client, timeframe: Timeframe, times: list[int]) -> None:
    for time in times:
        client.push_bar(WINZ25, timeframe, time=time, o=1, h=1, low=1, c=1, volume=1)


def read_all_bars(
    client: FakeMt5Client, timeframe: Timeframe, *, from_epoch: int, to_epoch: int, page_size: int
) -> list[list[int]]:
    plan = plan_bar_pages(
        timeframe=timeframe, from_epoch=from_epoch, to_epoch=to_epoch, page_size=page_size
    )
    return [
        [bar.time for bar in read_bar_page(client, WINZ25, timeframe, plan, page)]
        for page in range(1, plan.total_pages + 1)
    ]


class TestBarPagePlan:
    def test_the_page_count_follows_from_the_dates_and_the_page_size(self) -> None:
        plan = plan_bar_pages(
            timeframe=Timeframe.M1, from_epoch=0, to_epoch=2500 * MINUTE, page_size=1000
        )

        assert plan.total_pages == 3
        assert plan.window(1) == (0, 1000 * MINUTE)
        assert plan.window(3) == (2000 * MINUTE, 2500 * MINUTE)

    def test_a_range_that_fills_its_pages_exactly_has_no_extra_page(self) -> None:
        plan = plan_bar_pages(
            timeframe=Timeframe.H1, from_epoch=NOW, to_epoch=NOW + 48 * 3600, page_size=24
        )

        assert plan.total_pages == 2

    def test_without_a_page_size_a_page_is_a_thousand_bar_lengths(self) -> None:
        plan = plan_bar_pages(timeframe=Timeframe.M1, from_epoch=0, to_epoch=2500 * MINUTE)

        assert PAGE_SIZE == 1000
        assert (plan.page_size, plan.total_pages) == (1000, 3)

    def test_a_range_shorter_than_one_bar_is_one_page(self) -> None:
        plan = plan_bar_pages(timeframe=Timeframe.D1, from_epoch=NOW, to_epoch=NOW + 1, page_size=1)

        assert plan.total_pages == 1

    def test_a_window_of_monthly_bars_never_holds_more_than_the_page_size(self) -> None:
        plan = plan_bar_pages(
            timeframe=Timeframe.MN1, from_epoch=0, to_epoch=365 * DAY, page_size=1
        )

        start, end = plan.window(1)
        assert end - start == 28 * DAY

    @pytest.mark.parametrize(
        ("from_epoch", "to_epoch", "field"),
        [(-1, 10, "from_epoch"), (10, 10, "to_epoch"), (10, 5, "to_epoch")],
    )
    def test_a_range_that_cannot_be_read_names_the_field_at_fault(
        self, from_epoch: int, to_epoch: int, field: str
    ) -> None:
        with pytest.raises(InvalidRangeError) as refused:
            plan_bar_pages(timeframe=Timeframe.M1, from_epoch=from_epoch, to_epoch=to_epoch)

        assert refused.value.field == field

    @pytest.mark.parametrize("page", [0, 4])
    def test_a_page_outside_the_plan_is_refused(self, page: int) -> None:
        plan = plan_bar_pages(
            timeframe=Timeframe.M1, from_epoch=0, to_epoch=2500 * MINUTE, page_size=1000
        )

        with pytest.raises(InvalidRangeError) as refused:
            plan.window(page)

        assert refused.value.field == "page"


class TestReadBarPage:
    def test_walking_every_page_returns_each_bar_of_the_range_once_in_order(self) -> None:
        client = connected_client(FixedClock(NOW_MS))
        times = [NOW + i * MINUTE for i in range(25)]
        # One bar after the range: it closes the last one inside it.
        push_bars(client, Timeframe.M1, [*times, NOW + 25 * MINUTE])

        pages = read_all_bars(
            client, Timeframe.M1, from_epoch=NOW, to_epoch=NOW + 25 * MINUTE, page_size=10
        )

        assert [len(page) for page in pages] == [10, 10, 5]
        assert [time for page in pages for time in page] == times

    def test_the_range_starts_inclusive_and_ends_exclusive(self) -> None:
        client = connected_client(FixedClock(NOW_MS))
        push_bars(client, Timeframe.M1, [NOW + i * MINUTE for i in range(-1, 5)])

        pages = read_all_bars(
            client, Timeframe.M1, from_epoch=NOW, to_epoch=NOW + 3 * MINUTE, page_size=100
        )

        assert pages == [[NOW, NOW + MINUTE, NOW + 2 * MINUTE]]

    def test_a_window_without_bars_is_an_empty_page_and_not_the_end(self) -> None:
        client = connected_client(FixedClock(NOW_MS))
        after_the_gap = NOW + 30 * MINUTE
        push_bars(client, Timeframe.M1, [NOW, after_the_gap, after_the_gap + MINUTE])

        pages = read_all_bars(
            client, Timeframe.M1, from_epoch=NOW, to_epoch=NOW + 40 * MINUTE, page_size=10
        )

        assert pages == [[NOW], [], [], [after_the_gap]]

    def test_the_bar_still_forming_is_never_returned(self) -> None:
        client = connected_client(FixedClock(NOW_MS))
        push_bars(client, Timeframe.M1, [NOW, NOW + MINUTE])

        pages = read_all_bars(
            client, Timeframe.M1, from_epoch=NOW, to_epoch=NOW + 10 * MINUTE, page_size=100
        )

        assert pages == [[NOW]]


def push_ticks(client: FakeMt5Client, clock: FixedClock, offsets_ms: list[int]) -> list[int]:
    """One tick at each offset from NOW_MS; the price is its position."""
    for index, offset in enumerate(offsets_ms):
        clock.set_ms(NOW_MS + offset)
        price = Decimal(index)
        client.set_tick(WINZ25, bid=price, ask=price, last=price, volume=Decimal(1))
    return list(range(len(offsets_ms)))


def read_all_ticks(
    client: FakeMt5Client, *, from_time_msc: int, to_time_msc: int, limit: int
) -> list[list[int]]:
    """The prices of every page, following next_cursor to the end."""
    pages: list[list[int]] = []
    cursor: str | None = None
    while True:
        position = start_tick_page(
            from_time_msc=from_time_msc, to_time_msc=to_time_msc, cursor=cursor
        )
        page = read_tick_page(
            client,
            WINZ25,
            from_time_msc=from_time_msc,
            to_time_msc=to_time_msc,
            limit=limit,
            position=position,
        )
        pages.append([int(tick.bid) for tick in page.ticks])
        if page.next_cursor is None:
            return pages
        cursor = page.next_cursor.encode()
        assert len(pages) < 100, "the cursor does not advance"


class TestReadTickPage:
    def test_following_the_cursor_returns_each_tick_once_in_order(self) -> None:
        clock = FixedClock(NOW_MS)
        client = connected_client(clock)
        prices = push_ticks(client, clock, [i * 400 for i in range(11)])

        pages = read_all_ticks(client, from_time_msc=NOW_MS, to_time_msc=NOW_MS + 60_000, limit=4)

        assert [len(page) for page in pages] == [4, 4, 3]
        assert [price for page in pages for price in page] == prices

    def test_ticks_that_share_one_millisecond_are_neither_lost_nor_repeated(self) -> None:
        clock = FixedClock(NOW_MS)
        client = connected_client(clock)
        prices = push_ticks(client, clock, [0, 500, 500, 500, 500, 500, 1200])

        pages = read_all_ticks(client, from_time_msc=NOW_MS, to_time_msc=NOW_MS + 60_000, limit=2)

        assert [price for page in pages for price in page] == prices
        assert all(len(page) <= 2 for page in pages)

    def test_the_range_starts_inclusive_and_ends_exclusive(self) -> None:
        clock = FixedClock(NOW_MS)
        client = connected_client(clock)
        push_ticks(client, clock, [0, 1000, 2000, 3000])

        pages = read_all_ticks(
            client, from_time_msc=NOW_MS + 1000, to_time_msc=NOW_MS + 3000, limit=10
        )

        assert pages == [[1, 2]]

    def test_a_range_that_starts_inside_a_second_skips_the_ticks_before_it(self) -> None:
        clock = FixedClock(NOW_MS)
        client = connected_client(clock)
        push_ticks(client, clock, [100, 200, 300, 600, 700, 1500])

        pages = read_all_ticks(
            client, from_time_msc=NOW_MS + 250, to_time_msc=NOW_MS + 60_000, limit=2
        )

        assert [price for page in pages for price in page] == [2, 3, 4, 5]

    def test_a_page_that_ends_with_the_last_tick_has_no_next_cursor(self) -> None:
        clock = FixedClock(NOW_MS)
        client = connected_client(clock)
        push_ticks(client, clock, [0, 100])

        pages = read_all_ticks(client, from_time_msc=NOW_MS, to_time_msc=NOW_MS + 60_000, limit=2)

        assert pages == [[0, 1]]

    def test_a_range_without_ticks_is_one_empty_page(self) -> None:
        clock = FixedClock(NOW_MS)
        client = connected_client(clock)
        push_ticks(client, clock, [0])

        pages = read_all_ticks(
            client, from_time_msc=NOW_MS + 5000, to_time_msc=NOW_MS + 6000, limit=10
        )

        assert pages == [[]]

    def test_a_cursor_round_trips_through_its_text(self) -> None:
        cursor = TickCursor(anchor_msc=NOW_MS, skip=7)

        assert TickCursor.parse(cursor.encode()) == cursor

    @pytest.mark.parametrize("text", ["", "abc", "1700000000000", "1700000000001.0", "-1000.0"])
    def test_text_that_is_not_a_cursor_is_refused(self, text: str) -> None:
        with pytest.raises(InvalidRangeError) as refused:
            TickCursor.parse(text)

        assert refused.value.field == "cursor"

    @pytest.mark.parametrize(
        ("from_time_msc", "to_time_msc", "cursor", "field"),
        [
            (-1, 10, None, "from_time_msc"),
            (10, 10, None, "to_time_msc"),
            (NOW_MS, NOW_MS + 1000, f"{NOW_MS - 1000}.0", "cursor"),
            (NOW_MS, NOW_MS + 1000, f"{NOW_MS + 1000}.0", "cursor"),
        ],
    )
    def test_a_request_that_cannot_be_read_names_the_field_at_fault(
        self, from_time_msc: int, to_time_msc: int, cursor: str | None, field: str
    ) -> None:
        with pytest.raises(InvalidRangeError) as refused:
            start_tick_page(from_time_msc=from_time_msc, to_time_msc=to_time_msc, cursor=cursor)

        assert refused.value.field == field
