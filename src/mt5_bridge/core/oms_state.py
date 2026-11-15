"""The bridge journal's own order-lifecycle vocabulary.

A subset of plan §56's OMS states: the bridge does not do pre-trade risk
gating beyond its own ``LimitsGuard`` (so it has no ``CREATED``/
``RISK_CHECKED`` states — those belong to the client's OMS), but ``UNKNOWN`` is
mandatory here too, for exactly the same reason (§56, §114): a crash or a
lost response must never be silently resolved into a guess.
"""

from enum import StrEnum

from mt5_bridge.mt5.models import Mt5OrderState


class BridgeOrderState(StrEnum):
    RECEIVED = "RECEIVED"
    SUBMITTED = "SUBMITTED"
    ACKNOWLEDGED = "ACKNOWLEDGED"
    PARTIALLY_FILLED = "PARTIALLY_FILLED"
    FILLED = "FILLED"
    PENDING_CANCEL = "PENDING_CANCEL"
    PENDING_REPLACE = "PENDING_REPLACE"
    REPLACED = "REPLACED"
    REJECTED = "REJECTED"
    CANCELED = "CANCELED"
    EXPIRED = "EXPIRED"
    UNKNOWN = "UNKNOWN"


TERMINAL_STATES = frozenset(
    {
        BridgeOrderState.FILLED,
        BridgeOrderState.REJECTED,
        BridgeOrderState.CANCELED,
        BridgeOrderState.EXPIRED,
        BridgeOrderState.REPLACED,
    }
)
"""States after which the bridge expects no further terminal-side change.

``UNKNOWN`` is deliberately excluded: it blocks new orders on the same
instrument until reconciled (§56), which a truly terminal state does not.
"""


class ReplaceOutcome(StrEnum):
    AMENDED = "AMENDED"
    REPLACED = "REPLACED"
    CANCEL_FAILED = "CANCEL_FAILED"
    CANCELED_NEW_REJECTED = "CANCELED_NEW_REJECTED"
    BOTH_OPEN = "BOTH_OPEN"
    UNKNOWN = "UNKNOWN"
    PENDING = "PENDING"
    """Bridge-internal bookkeeping value between the two legs; never returned
    to a caller as a final outcome."""


_TERMINAL_STATE_BY_MT5_STATE: dict[Mt5OrderState, BridgeOrderState] = {
    Mt5OrderState.STARTED: BridgeOrderState.ACKNOWLEDGED,
    Mt5OrderState.PLACED: BridgeOrderState.ACKNOWLEDGED,
    Mt5OrderState.PARTIAL: BridgeOrderState.PARTIALLY_FILLED,
    Mt5OrderState.FILLED: BridgeOrderState.FILLED,
    Mt5OrderState.CANCELED: BridgeOrderState.CANCELED,
    Mt5OrderState.REJECTED: BridgeOrderState.REJECTED,
    Mt5OrderState.EXPIRED: BridgeOrderState.EXPIRED,
    Mt5OrderState.REQUEST_ADD: BridgeOrderState.UNKNOWN,
    Mt5OrderState.REQUEST_MODIFY: BridgeOrderState.UNKNOWN,
    Mt5OrderState.REQUEST_CANCEL: BridgeOrderState.UNKNOWN,
}


def map_terminal_state(state: Mt5OrderState) -> BridgeOrderState:
    """Translate the terminal's own order state into the journal's vocabulary.

    A mid-transition MT5 state (``REQUEST_*``) maps to ``UNKNOWN`` rather than
    a guess: those states mean the terminal itself has not settled yet.
    """
    return _TERMINAL_STATE_BY_MT5_STATE.get(state, BridgeOrderState.UNKNOWN)
