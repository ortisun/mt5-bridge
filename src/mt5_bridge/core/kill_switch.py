"""Persistent, escalation-only kill switch (plan §107, §114).

Stored in the same VM-local SQLite file as the order journal, so a bridge
restart never clears it — rearming requires an explicit call.
Activating a lower-severity action while a higher one is already active does
not downgrade it: only ``rearm`` returns to ``NORMAL``.
"""

import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum

from mt5_bridge.core.database import BridgeDatabase
from mt5_bridge.core.metrics_port import Metrics


class KillSwitchState(StrEnum):
    NORMAL = "NORMAL"
    HALTED = "HALTED"
    CANCELED_ALL = "CANCELED_ALL"
    FLATTENED = "FLATTENED"


class KillSwitchAction(StrEnum):
    HALT_NEW_ORDERS = "HALT_NEW_ORDERS"
    CANCEL_ALL = "CANCEL_ALL"
    FLATTEN = "FLATTEN"


_TARGET_STATE: dict[KillSwitchAction, KillSwitchState] = {
    KillSwitchAction.HALT_NEW_ORDERS: KillSwitchState.HALTED,
    KillSwitchAction.CANCEL_ALL: KillSwitchState.CANCELED_ALL,
    KillSwitchAction.FLATTEN: KillSwitchState.FLATTENED,
}

_SEVERITY: dict[KillSwitchState, int] = {
    KillSwitchState.NORMAL: 0,
    KillSwitchState.HALTED: 1,
    KillSwitchState.CANCELED_ALL: 2,
    KillSwitchState.FLATTENED: 3,
}


# The severity order above as a JSON object, bound into the escalation statement so
# SQL compares severities from this one source, never from a second copy of the order.
_SEVERITY_BY_STATE_JSON = json.dumps({state.value: rank for state, rank in _SEVERITY.items()})

# Compare-and-set: the row changes only while its stored severity is below the target's,
# decided by SQLite against the current row, not against anything this process read.
_ESCALATE_SQL = (
    "UPDATE kill_switch_state SET state = ?, reason = ?, updated_at = ? "
    "WHERE id = 1 AND json_extract(?, '$.' || state) < ?"
)
_SELECT_SQL = "SELECT state, reason, updated_at FROM kill_switch_state WHERE id = 1"


@dataclass(frozen=True, slots=True)
class KillSwitchStatus:
    state: KillSwitchState
    reason: str | None
    updated_at: datetime


def _status_of(row: sqlite3.Row) -> KillSwitchStatus:
    return KillSwitchStatus(
        state=KillSwitchState(row["state"]),
        reason=row["reason"],
        updated_at=datetime.fromisoformat(row["updated_at"]),
    )


class KillSwitch:
    def __init__(self, db: BridgeDatabase, *, now: datetime, metrics: Metrics) -> None:
        self._db = db
        self._metrics = metrics
        with self._db.transaction() as conn:
            conn.execute(
                "INSERT OR IGNORE INTO kill_switch_state (id, state, reason, updated_at) "
                "VALUES (1, ?, NULL, ?)",
                (KillSwitchState.NORMAL.value, now.isoformat()),
            )

    def status(self) -> KillSwitchStatus:
        row = self._db.fetchone(_SELECT_SQL)
        if row is None:  # pragma: no cover - __init__ always inserts the row
            raise RuntimeError("kill_switch_state row missing after initialization")
        return _status_of(row)

    def activate(self, action: KillSwitchAction, *, reason: str, now: datetime) -> KillSwitchStatus:
        """Escalate to ``action``'s state unless a higher or equal one is already in force.

        One conditional UPDATE does the escalation, so a second caller (another
        thread, handle or process on the same file) that stored a higher state
        first is never overwritten, and an activation that does not escalate
        writes nothing, ``updated_at`` included. The returned status is the
        stored row. ``previous`` is read just before the write and only labels
        the transition metric: under a cross-process race it can be stale, the
        stored state cannot.
        """
        target = _TARGET_STATE[action]
        with self._db.transaction() as conn:
            before = conn.execute(_SELECT_SQL).fetchone()
            escalated = (
                conn.execute(
                    _ESCALATE_SQL,
                    (
                        target.value,
                        reason,
                        now.isoformat(),
                        _SEVERITY_BY_STATE_JSON,
                        _SEVERITY[target],
                    ),
                ).rowcount
                == 1
            )
            after = conn.execute(_SELECT_SQL).fetchone()
        if before is None or after is None:  # pragma: no cover - __init__ always inserts the row
            raise RuntimeError("kill_switch_state row missing after initialization")
        if escalated:
            self._metrics.increment(
                "mt5_bridge.kill_switch.transition",
                from_state=before["state"],
                to_state=target.value,
            )
        return _status_of(after)

    def rearm(self, *, now: datetime) -> KillSwitchStatus:
        """Return to NORMAL. Never triggered automatically."""
        current = self.status()
        with self._db.transaction() as conn:
            conn.execute(
                "UPDATE kill_switch_state SET state = ?, reason = NULL, updated_at = ? "
                "WHERE id = 1",
                (KillSwitchState.NORMAL.value, now.isoformat()),
            )
        if current.state is not KillSwitchState.NORMAL:
            self._metrics.increment(
                "mt5_bridge.kill_switch.transition",
                from_state=current.state.value,
                to_state=KillSwitchState.NORMAL.value,
            )
        return KillSwitchStatus(state=KillSwitchState.NORMAL, reason=None, updated_at=now)

    def blocks_new_orders(self) -> bool:
        return self.status().state is not KillSwitchState.NORMAL
