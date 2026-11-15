"""Turn a raw, source-declared epoch into a UTC-aware instant.

Plan rule 30: every external source's time unit is declared and validated
during canonicalization; it is never inferred from magnitude. ``to_utc``
takes the unit ``mt5.models`` already declares for that field (e.g.
``Tick.time_msc`` is milliseconds, ``Bar.time`` is seconds — §116) and does
nothing but arithmetic: no timezone-offset guessing, no "does this look like
seconds or milliseconds" heuristic.
"""

from datetime import UTC, datetime

from mt5_bridge.mt5.models import SourceTimeUnit

_UNITS_PER_SECOND: dict[SourceTimeUnit, int] = {
    SourceTimeUnit.SECONDS: 1,
    SourceTimeUnit.MILLISECONDS: 1_000,
}


def to_utc(raw: int, unit: SourceTimeUnit) -> datetime:
    """Convert ``raw`` (already known to be in ``unit``) to UTC.

    Never call this to *guess* a unit from ``raw``'s size; the unit must
    already be known from where the value came from.
    """
    seconds = raw / _UNITS_PER_SECOND[unit]
    return datetime.fromtimestamp(seconds, tz=UTC)
