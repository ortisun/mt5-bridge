"""TimeNormalizer: UTC from a declared unit, never an inferred offset."""

from datetime import UTC, datetime

import pytest

from mt5_bridge.core.time_normalizer import to_utc
from mt5_bridge.mt5.models import SourceTimeUnit


def test_seconds_epoch_converts_to_the_exact_known_utc_instant() -> None:
    result = to_utc(1_700_000_000, SourceTimeUnit.SECONDS)

    assert result == datetime(2023, 11, 14, 22, 13, 20, tzinfo=UTC)


def test_milliseconds_epoch_converts_to_the_same_instant_as_seconds() -> None:
    from_seconds = to_utc(1_700_000_000, SourceTimeUnit.SECONDS)
    from_millis = to_utc(1_700_000_000_000, SourceTimeUnit.MILLISECONDS)

    assert from_seconds == from_millis


def test_result_is_always_utc_aware_never_naive() -> None:
    result = to_utc(0, SourceTimeUnit.SECONDS)

    assert result.tzinfo is UTC


@pytest.mark.parametrize("unit", [SourceTimeUnit.SECONDS, SourceTimeUnit.MILLISECONDS])
def test_one_hour_apart_raw_values_are_exactly_one_hour_apart_in_utc(
    unit: SourceTimeUnit,
) -> None:
    """Spans a real DST transition instant; UTC arithmetic must not notice.

    2024-03-10 07:00:00 UTC is during the US spring-forward transition
    (2am->3am America/New_York on that date). A normalizer that leaked any
    local-time arithmetic would not show a clean one-hour delta here.
    """
    scale = 1 if unit is SourceTimeUnit.SECONDS else 1000
    before = to_utc(1_710_054_000 * scale, unit)
    after = to_utc(1_710_057_600 * scale, unit)

    assert (after - before).total_seconds() == 3600


def test_zero_is_the_unix_epoch_not_a_sentinel_for_missing_data() -> None:
    result = to_utc(0, SourceTimeUnit.SECONDS)

    assert result == datetime(1970, 1, 1, tzinfo=UTC)
