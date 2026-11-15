"""LimitsGuard: the bridge's own last line of defense, independent of any client."""

from decimal import Decimal

from mt5_bridge.core.limits_guard import LimitsConfig, LimitsGuard, LimitsState

DEFAULT_STATE = LimitsState(
    open_notional=Decimal(0), daily_loss=Decimal(0), orders_in_last_minute=0
)


def make_guard(**overrides: object) -> LimitsGuard:
    defaults: dict[str, object] = {
        "symbol_allowlist": frozenset({"WINZ25"}),
        "max_volume_per_order": Decimal(10),
        "max_orders_per_minute": 5,
        "max_open_notional": Decimal(100_000),
        "max_daily_loss": Decimal(5_000),
    }
    defaults.update(overrides)
    return LimitsGuard(LimitsConfig(**defaults))  # type: ignore[arg-type]


def test_within_every_limit_is_approved() -> None:
    guard = make_guard()

    decision = guard.check(
        symbol="WINZ25", volume=Decimal(1), estimated_notional=Decimal(1000), state=DEFAULT_STATE
    )

    assert decision.approved is True
    assert decision.reasons == ()


def test_symbol_outside_the_allowlist_is_rejected() -> None:
    guard = make_guard(symbol_allowlist=frozenset({"PETR4"}))

    decision = guard.check(
        symbol="WINZ25", volume=Decimal(1), estimated_notional=Decimal(1), state=DEFAULT_STATE
    )

    assert decision.approved is False
    assert any("allowlist" in reason for reason in decision.reasons)


def test_a_none_allowlist_permits_any_symbol() -> None:
    guard = make_guard(symbol_allowlist=None)

    decision = guard.check(
        symbol="ANYTHING", volume=Decimal(1), estimated_notional=Decimal(1), state=DEFAULT_STATE
    )

    assert decision.approved is True


def test_volume_over_the_per_order_maximum_is_rejected() -> None:
    guard = make_guard(max_volume_per_order=Decimal(1))

    decision = guard.check(
        symbol="WINZ25", volume=Decimal(2), estimated_notional=Decimal(1), state=DEFAULT_STATE
    )

    assert decision.approved is False


def test_orders_per_minute_at_the_limit_is_rejected() -> None:
    guard = make_guard(max_orders_per_minute=3)
    state = LimitsState(open_notional=Decimal(0), daily_loss=Decimal(0), orders_in_last_minute=3)

    decision = guard.check(
        symbol="WINZ25", volume=Decimal(1), estimated_notional=Decimal(1), state=state
    )

    assert decision.approved is False


def test_open_notional_that_would_exceed_the_cap_is_rejected() -> None:
    guard = make_guard(max_open_notional=Decimal(1000))
    state = LimitsState(open_notional=Decimal(900), daily_loss=Decimal(0), orders_in_last_minute=0)

    decision = guard.check(
        symbol="WINZ25", volume=Decimal(1), estimated_notional=Decimal(200), state=state
    )

    assert decision.approved is False


def test_daily_loss_already_at_the_cap_is_rejected() -> None:
    guard = make_guard(max_daily_loss=Decimal(500))
    state = LimitsState(open_notional=Decimal(0), daily_loss=Decimal(500), orders_in_last_minute=0)

    decision = guard.check(
        symbol="WINZ25", volume=Decimal(1), estimated_notional=Decimal(1), state=state
    )

    assert decision.approved is False


def test_stale_daily_loss_evidence_rejects_even_an_otherwise_clean_order() -> None:
    guard = make_guard()
    state = LimitsState(
        open_notional=Decimal(0),
        daily_loss=Decimal(0),
        orders_in_last_minute=0,
        daily_loss_evidence_stale=True,
    )

    decision = guard.check(
        symbol="WINZ25", volume=Decimal(1), estimated_notional=Decimal(1), state=state
    )

    assert decision.approved is False
    assert any("stale" in reason for reason in decision.reasons)


def test_fresh_daily_loss_evidence_does_not_add_a_rejection_reason() -> None:
    guard = make_guard()

    decision = guard.check(
        symbol="WINZ25", volume=Decimal(1), estimated_notional=Decimal(1), state=DEFAULT_STATE
    )

    assert decision.approved is True


class TestReduceOnlySkipsExposureAndLossRules:
    def test_reduce_only_is_approved_over_max_open_notional(self) -> None:
        guard = make_guard(max_open_notional=Decimal(1000))
        state = LimitsState(
            open_notional=Decimal(900), daily_loss=Decimal(0), orders_in_last_minute=0
        )

        decision = guard.check(
            symbol="WINZ25",
            volume=Decimal(1),
            estimated_notional=Decimal(200),
            state=state,
            reduce_only=True,
        )

        assert decision.approved is True

    def test_reduce_only_is_approved_over_max_daily_loss_already_reached(self) -> None:
        guard = make_guard(max_daily_loss=Decimal(500))
        state = LimitsState(
            open_notional=Decimal(0), daily_loss=Decimal(500), orders_in_last_minute=0
        )

        decision = guard.check(
            symbol="WINZ25",
            volume=Decimal(1),
            estimated_notional=Decimal(1),
            state=state,
            reduce_only=True,
        )

        assert decision.approved is True

    def test_reduce_only_is_approved_over_stale_daily_loss_evidence(self) -> None:
        guard = make_guard()
        state = LimitsState(
            open_notional=Decimal(0),
            daily_loss=Decimal(0),
            orders_in_last_minute=0,
            daily_loss_evidence_stale=True,
        )

        decision = guard.check(
            symbol="WINZ25",
            volume=Decimal(1),
            estimated_notional=Decimal(1),
            state=state,
            reduce_only=True,
        )

        assert decision.approved is True

    def test_reduce_only_still_respects_the_symbol_allowlist(self) -> None:
        guard = make_guard(symbol_allowlist=frozenset({"PETR4"}))

        decision = guard.check(
            symbol="WINZ25",
            volume=Decimal(1),
            estimated_notional=Decimal(1),
            state=DEFAULT_STATE,
            reduce_only=True,
        )

        assert decision.approved is False
        assert any("allowlist" in reason for reason in decision.reasons)

    def test_reduce_only_still_respects_max_volume_per_order(self) -> None:
        guard = make_guard(max_volume_per_order=Decimal(1))

        decision = guard.check(
            symbol="WINZ25",
            volume=Decimal(2),
            estimated_notional=Decimal(1),
            state=DEFAULT_STATE,
            reduce_only=True,
        )

        assert decision.approved is False

    def test_reduce_only_still_respects_max_orders_per_minute(self) -> None:
        guard = make_guard(max_orders_per_minute=3)
        state = LimitsState(
            open_notional=Decimal(0), daily_loss=Decimal(0), orders_in_last_minute=3
        )

        decision = guard.check(
            symbol="WINZ25",
            volume=Decimal(1),
            estimated_notional=Decimal(1),
            state=state,
            reduce_only=True,
        )

        assert decision.approved is False


def test_multiple_violations_are_all_reported() -> None:
    guard = make_guard(max_volume_per_order=Decimal(1), symbol_allowlist=frozenset({"PETR4"}))

    decision = guard.check(
        symbol="WINZ25", volume=Decimal(5), estimated_notional=Decimal(1), state=DEFAULT_STATE
    )

    assert len(decision.reasons) == 2
