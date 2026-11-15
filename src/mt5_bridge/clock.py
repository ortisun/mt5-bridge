"""The bridge's only source of "now". Every timed behavior takes one of these.

Injected everywhere a component needs the current instant, so tests drive
time explicitly instead of sleeping (AGENTS.md: deterministic tests only).
"""

from datetime import UTC, datetime
from time import time_ns
from typing import Protocol


class Clock(Protocol):
    def now_utc(self) -> datetime:
        """The current instant, timezone-aware in UTC."""
        ...

    def now_ms(self) -> int:
        """The current instant as milliseconds since the Unix epoch, UTC."""
        ...


class SystemClock:
    """Wall-clock time. The only clock ``main.py`` wires in production."""

    def now_utc(self) -> datetime:
        return datetime.now(UTC)

    def now_ms(self) -> int:
        return time_ns() // 1_000_000
