"""Clock drift: the threshold check, and measuring into a shared holder."""

from mt5_bridge.core.clock_drift import ClockDriftState, measure_drift_into, within_threshold


class TestWithinThreshold:
    def test_zero_drift_is_always_within_any_nonnegative_threshold(self) -> None:
        assert within_threshold(0.0, 0.0) is True

    def test_drift_at_exactly_the_threshold_is_within(self) -> None:
        assert within_threshold(2.0, 2.0) is True
        assert within_threshold(-2.0, 2.0) is True

    def test_drift_past_the_threshold_is_not_within(self) -> None:
        assert within_threshold(2.1, 2.0) is False
        assert within_threshold(-2.1, 2.0) is False


class TestMeasureDriftInto:
    def test_a_successful_probe_updates_the_state(self) -> None:
        state = ClockDriftState()

        measure_drift_into(state, probe=lambda: 1.5)

        assert state.drift_seconds == 1.5

    def test_a_failing_probe_leaves_the_last_known_value(self) -> None:
        state = ClockDriftState(drift_seconds=0.7)

        def failing_probe() -> float:
            raise TimeoutError("no network")

        measure_drift_into(state, probe=failing_probe)

        assert state.drift_seconds == 0.7
