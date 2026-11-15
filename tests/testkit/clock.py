"""A controllable ``Clock`` for deterministic, sleep-free tests."""

from datetime import UTC, datetime

from mt5_bridge.clock import Clock


class FixedClock:
    """Implements ``Clock``; only moves when a test tells it to."""

    def __init__(self, start_ms: int) -> None:
        self._now_ms = start_ms

    def now_ms(self) -> int:
        return self._now_ms

    def now_utc(self) -> datetime:
        return datetime.fromtimestamp(self._now_ms / 1000, tz=UTC)

    def advance(self, seconds: float) -> None:
        if seconds < 0:
            raise ValueError("A clock cannot move backwards.")
        self._now_ms += round(seconds * 1000)

    def set_ms(self, now_ms: int) -> None:
        self._now_ms = now_ms


def _assert_protocol(clock: Clock) -> None:
    """Compile-time-only: keeps ``FixedClock`` honest against ``Clock``."""
    _ = clock


_assert_protocol(FixedClock(0))
