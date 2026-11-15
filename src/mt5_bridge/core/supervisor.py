"""Boot and ongoing safety supervision (plan §114 "B3 Specifics", §107).

``verify_netting_mode`` is a boot gate: a mismatch between the terminal's
actual account mode and what this deployment is configured for is a hard
failure, not a warning. ``sweep_dead_man_switch`` is the periodic check that
turns an expired lease into ``CANCEL_ALL`` plus a persisted kill-switch
transition, so the halt survives even if the sweep itself is not called
again for a while.
"""

from dataclasses import dataclass

from mt5_bridge.clock import Clock
from mt5_bridge.core.dead_man_switch import DeadManSwitch
from mt5_bridge.core.emergency_actions import CancelAllReport, cancel_all_open_orders
from mt5_bridge.core.kill_switch import KillSwitch, KillSwitchAction
from mt5_bridge.mt5.client import Mt5Client
from mt5_bridge.mt5.models import AccountTradeMode, PositionAccounting


class NettingModeMismatch(Exception):
    def __init__(self, *, expected: PositionAccounting, actual: PositionAccounting) -> None:
        super().__init__(
            f"account_info().position_accounting is {actual.value}, "
            f"but this deployment expects {expected.value}"
        )
        self.expected = expected
        self.actual = actual


def verify_netting_mode(client: Mt5Client, *, expected: PositionAccounting) -> None:
    """Plan §114: "check account_info().margin_mode at boot; a mismatch with
    MarketContext.position_accounting is a failure." Raises rather than
    logging: a misconfigured account mode invalidates every quantity this
    bridge would otherwise report."""
    account = client.account_info()
    if account.position_accounting is not expected:
        raise NettingModeMismatch(expected=expected, actual=account.position_accounting)


class AccountBindingMismatch(Exception):
    """The terminal is on an account this deployment is not bound to."""


# Plan §114: paper runs only against a demo account, validated at boot;
# real-account access exists only in the live domain.
_ALLOWED_TRADE_MODES: dict[str, frozenset[AccountTradeMode]] = {
    "demo": frozenset({AccountTradeMode.DEMO}),
    "paper": frozenset({AccountTradeMode.DEMO}),
    "live": frozenset({AccountTradeMode.REAL}),
}


def verify_account_binding(
    client: Mt5Client,
    *,
    environment: str,
    expected_login: int | None,
    expected_server: str | None,
) -> None:
    """Boot gate: the attached terminal must be on the configured account kind.

    An environment outside the known set fails closed; the login and server
    are compared when configured (attach mode requires both, see
    ``main.resolve_terminal_connection``).
    """
    allowed = _ALLOWED_TRADE_MODES.get(environment)
    if allowed is None:
        raise AccountBindingMismatch(f"unknown bridge environment {environment!r}")
    account = client.account_info()
    if account.trade_mode not in allowed:
        raise AccountBindingMismatch(
            f"account trade mode {account.trade_mode.value} is not allowed "
            f"for the {environment!r} environment"
        )
    if expected_login is not None and account.login != expected_login:
        raise AccountBindingMismatch("terminal login differs from MT5_BRIDGE_EXPECTED_LOGIN")
    if expected_server is not None and account.server != expected_server:
        raise AccountBindingMismatch(
            f"terminal server {account.server!r} differs from MT5_BRIDGE_EXPECTED_SERVER"
        )


@dataclass(frozen=True, slots=True)
class DeadManSweepResult:
    expired: bool
    cancel_report: CancelAllReport | None


def sweep_dead_man_switch(
    *, switch: DeadManSwitch, kill_switch: KillSwitch, client: Mt5Client, clock: Clock
) -> DeadManSweepResult:
    if not switch.is_expired(clock.now_ms()):
        return DeadManSweepResult(expired=False, cancel_report=None)
    report = cancel_all_open_orders(client)
    kill_switch.activate(
        KillSwitchAction.CANCEL_ALL, reason="dead-man switch lease expired", now=clock.now_utc()
    )
    switch.disarm()
    return DeadManSweepResult(expired=True, cancel_report=report)
