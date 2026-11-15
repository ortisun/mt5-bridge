"""Reads bars and ticks by date range, one page at a time.

The terminal answers a range in one call (``copy_rates_range``,
``copy_ticks_range``), with no bound on the size of the answer: a year of M1
bars or a day of ticks is one array, built while the single terminal worker
serves nobody else. This module cuts a range into pages the bridge can answer
one call at a time. A caller names a range, never a quantity: the size of a
page is the bridge's own (``PAGE_SIZE``).

Bars: the number of pages follows from the dates alone. Two bars of one
timeframe open at least one bar length apart, so a time window of
``page_size`` bar lengths holds at most ``page_size`` bars. The range is cut
into such windows and each page is one window. The count is an upper bound of
the pages that hold data: a window over a weekend or a holiday is an empty
page, not the end of the range.

Ticks: the number of ticks in a period is not known before reading them, so
there is no page count. A page carries a cursor to the next one instead.
"""

import re
from dataclasses import dataclass

from mt5_bridge.mt5.client import Mt5Client
from mt5_bridge.mt5.models import Bar, Tick, Timeframe

# The most bars or ticks one page holds. Not a request parameter: a caller
# asks for a range, never for a quantity.
PAGE_SIZE = 1000

_MINUTE = 60
_DAY = 24 * 60 * _MINUTE
# The least time between the opens of two consecutive bars. A month is taken
# as its shortest, 28 days, so that a window never holds more than page_size
# monthly bars.
_MIN_BAR_SPACING_SECONDS: dict[Timeframe, int] = {
    Timeframe.M1: _MINUTE,
    Timeframe.M5: 5 * _MINUTE,
    Timeframe.M15: 15 * _MINUTE,
    Timeframe.M30: 30 * _MINUTE,
    Timeframe.H1: 60 * _MINUTE,
    Timeframe.H4: 4 * 60 * _MINUTE,
    Timeframe.D1: _DAY,
    Timeframe.W1: 7 * _DAY,
    Timeframe.MN1: 28 * _DAY,
}


class InvalidRangeError(ValueError):
    """A range, page or cursor the caller must correct; ``field`` names it."""

    def __init__(self, field: str, detail: str) -> None:
        super().__init__(f"{field}: {detail}")
        self.field = field
        self.detail = detail


@dataclass(frozen=True, slots=True)
class BarPagePlan:
    """How the range ``from_epoch <= open time < to_epoch`` is cut into pages."""

    from_epoch: int
    to_epoch: int
    page_size: int
    window_seconds: int
    total_pages: int

    def window(self, page: int) -> tuple[int, int]:
        """The open times of page ``page`` (1-based): ``start <= time < end``."""
        if not 1 <= page <= self.total_pages:
            raise InvalidRangeError("page", f"must be between 1 and {self.total_pages}")
        start = self.from_epoch + (page - 1) * self.window_seconds
        return start, min(start + self.window_seconds, self.to_epoch)


def plan_bar_pages(
    *, timeframe: Timeframe, from_epoch: int, to_epoch: int, page_size: int = PAGE_SIZE
) -> BarPagePlan:
    if from_epoch < 0:
        raise InvalidRangeError("from_epoch", "must not be negative")
    if to_epoch <= from_epoch:
        raise InvalidRangeError("to_epoch", "must be later than from_epoch")
    window_seconds = page_size * _MIN_BAR_SPACING_SECONDS[timeframe]
    return BarPagePlan(
        from_epoch=from_epoch,
        to_epoch=to_epoch,
        page_size=page_size,
        window_seconds=window_seconds,
        total_pages=-(-(to_epoch - from_epoch) // window_seconds),
    )


def read_bar_page(
    client: Mt5Client, symbol: str, timeframe: Timeframe, plan: BarPagePlan, page: int
) -> tuple[Bar, ...]:
    """The closed bars of one page, oldest first (rule 28: never the forming bar)."""
    start, end = plan.window(page)
    return tuple(
        bar for bar in client.copy_bars_range(symbol, timeframe, start, end) if bar.is_closed
    )


_CURSOR_PATTERN = re.compile(r"(\d{1,16})\.(\d{1,9})")


@dataclass(frozen=True, slots=True)
class TickCursor:
    """Where the next page of ticks starts.

    The terminal takes the start of a tick request in whole seconds, and
    several ticks can carry one ``time_msc``. So the position is a whole
    second (``anchor_msc``) and how many of the ticks from that second on were
    already read (``skip``), not the time of the last tick.
    """

    anchor_msc: int
    skip: int

    def encode(self) -> str:
        return f"{self.anchor_msc}.{self.skip}"

    @classmethod
    def parse(cls, text: str) -> TickCursor:
        match = _CURSOR_PATTERN.fullmatch(text)
        if match is None or int(match[1]) % 1000 != 0:
            raise InvalidRangeError("cursor", "is not a cursor this bridge returned")
        return cls(anchor_msc=int(match[1]), skip=int(match[2]))


@dataclass(frozen=True, slots=True)
class TickPage:
    ticks: tuple[Tick, ...]
    next_cursor: TickCursor | None
    """``None`` when the range has been read to its end."""


def start_tick_page(*, from_time_msc: int, to_time_msc: int, cursor: str | None) -> TickCursor:
    """Where a page request starts reading, after checking the request:
    ``cursor`` as the previous page returned it, or the start of the range."""
    if from_time_msc < 0:
        raise InvalidRangeError("from_time_msc", "must not be negative")
    if to_time_msc <= from_time_msc:
        raise InvalidRangeError("to_time_msc", "must be later than from_time_msc")
    start_anchor = from_time_msc // 1000 * 1000
    if cursor is None:
        return TickCursor(anchor_msc=start_anchor, skip=0)
    position = TickCursor.parse(cursor)
    if not start_anchor <= position.anchor_msc < to_time_msc:
        raise InvalidRangeError("cursor", "does not belong to this range")
    return position


def read_tick_page(
    client: Mt5Client,
    symbol: str,
    *,
    from_time_msc: int,
    to_time_msc: int,
    position: TickCursor,
    limit: int = PAGE_SIZE,
) -> TickPage:
    """Up to ``limit`` ticks with ``from_time_msc <= time_msc < to_time_msc``,
    oldest first, read from ``position`` (see ``start_tick_page``).

    A page can hold fewer than ``limit`` ticks, or none, and still have a next
    page: only ``next_cursor`` says whether the range is finished.
    """
    # One tick more than the page needs shows whether another page follows.
    requested = position.skip + limit + 1
    raw = client.copy_ticks(symbol, position.anchor_msc, requested)

    consumed = min(position.skip, len(raw))
    page: list[Tick] = []
    reached_end = False
    while consumed < len(raw) and len(page) < limit:
        tick = raw[consumed]
        if tick.time_msc >= to_time_msc:
            reached_end = True
            break
        consumed += 1
        # Ticks of the first second that precede from_time_msc are read, not returned.
        if tick.time_msc >= from_time_msc:
            page.append(tick)

    if reached_end or consumed == 0:
        more = False
    elif consumed < len(raw):
        more = raw[consumed].time_msc < to_time_msc
    else:
        # Everything the terminal sent was read. It sent all that was asked
        # for only when it may hold more.
        more = len(raw) == requested
    if not more:
        return TickPage(ticks=tuple(page), next_cursor=None)

    anchor_msc = raw[consumed - 1].time_msc // 1000 * 1000
    skip = sum(1 for tick in raw[:consumed] if tick.time_msc >= anchor_msc)
    return TickPage(ticks=tuple(page), next_cursor=TickCursor(anchor_msc=anchor_msc, skip=skip))
