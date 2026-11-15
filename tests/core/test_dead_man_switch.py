"""DeadManSwitch: every heartbeat (re)establishes the lease."""

from mt5_bridge.core.dead_man_switch import DeadManSwitch

TIMEOUT_S = 30.0


def test_fresh_switch_is_not_expired_before_any_heartbeat() -> None:
    switch = DeadManSwitch(timeout_seconds=TIMEOUT_S)

    assert switch.is_expired(now_ms=0) is False
    assert switch.armed is False


def test_a_heartbeat_arms_the_lease() -> None:
    switch = DeadManSwitch(timeout_seconds=TIMEOUT_S)

    switch.heartbeat(now_ms=0)

    assert switch.armed is True
    assert switch.is_expired(now_ms=0) is False


def test_lease_expires_after_the_timeout_without_renewal() -> None:
    switch = DeadManSwitch(timeout_seconds=TIMEOUT_S)
    switch.heartbeat(now_ms=0)

    assert switch.is_expired(now_ms=29_999) is False
    assert switch.is_expired(now_ms=30_000) is True


def test_renewal_before_expiry_extends_the_deadline() -> None:
    switch = DeadManSwitch(timeout_seconds=TIMEOUT_S)
    switch.heartbeat(now_ms=0)

    switch.heartbeat(now_ms=20_000)

    assert switch.is_expired(now_ms=30_000) is False
    assert switch.is_expired(now_ms=50_000) is True


def test_a_heartbeat_after_expiry_establishes_a_new_lease() -> None:
    switch = DeadManSwitch(timeout_seconds=TIMEOUT_S)
    switch.heartbeat(now_ms=0)

    switch.heartbeat(now_ms=30_000)

    assert switch.is_expired(now_ms=30_000) is False
    assert switch.is_expired(now_ms=60_000) is True


def test_disarm_consumes_the_lease_until_the_next_heartbeat() -> None:
    switch = DeadManSwitch(timeout_seconds=TIMEOUT_S)
    switch.heartbeat(now_ms=0)

    switch.disarm()

    assert switch.armed is False
    assert switch.is_expired(now_ms=30_000) is False
