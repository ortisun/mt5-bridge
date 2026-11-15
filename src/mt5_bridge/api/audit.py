"""Append-only audit log of every write request (plan §112, §114).

Rows are only ever inserted, never updated or deleted, through the same
``BridgeDatabase`` transaction primitive as everything else. Route handlers
call ``record`` explicitly after a write completes (success or failure) so
the recorded ``outcome`` reflects what actually happened, not just that a
request arrived.

Each record is an OCSF API Activity event (``AuditRecord.to_ocsf``,
``docs/ocsf.md``): the method and path are the API operation and the HTTP
request, the peer address is the source endpoint and the outcome sets the
status. The bridge does not identify its callers, so the actor is the
terminal account the request acts on. A gRPC call is recorded with the method
``GRPC`` and the path ``/<package>.<Service>/<Method>``; the name of the RPC
sets the activity. ``record`` stores the row and then
writes the same event to the ``mt5_bridge.audit`` logger, so the audit trail
also reaches whatever the log handlers feed.
"""

import logging
import sqlite3
from dataclasses import dataclass
from datetime import datetime

from mt5_bridge.core import ocsf
from mt5_bridge.core.database import BridgeDatabase
from mt5_bridge.core.oms_state import BridgeOrderState, ReplaceOutcome

_LOG = logging.getLogger("mt5_bridge.audit")

AUDIT_EVENT_CODE = "audit"
# The ``method`` of a record made by the gRPC API; every other one is an HTTP method.
GRPC_METHOD = "GRPC"

# Outcomes that mean the request did not do what it asked for, and the one
# that means nobody knows yet (plan §56). Every other outcome is a success.
_FAILED_OUTCOMES = frozenset(
    {
        BridgeOrderState.REJECTED.value,
        ReplaceOutcome.CANCEL_FAILED.value,
        ReplaceOutcome.CANCELED_NEW_REJECTED.value,
        ReplaceOutcome.BOTH_OPEN.value,
    }
)
_UNDETERMINED_OUTCOMES = frozenset({BridgeOrderState.UNKNOWN.value})


def _status_of(outcome: str) -> tuple[ocsf.Status, ocsf.Severity]:
    if outcome in _FAILED_OUTCOMES:
        return ocsf.Status.FAILURE, ocsf.Severity.LOW
    if outcome in _UNDETERMINED_OUTCOMES:
        return ocsf.Status.UNKNOWN, ocsf.Severity.MEDIUM
    return ocsf.Status.SUCCESS, ocsf.Severity.INFORMATIONAL


@dataclass(frozen=True, slots=True)
class AuditRecord:
    ts_utc: datetime
    account_id: str | None
    method: str
    path: str
    payload_hash: str | None
    outcome: str
    detail: str | None
    src_host: str | None = None
    src_port: int | None = None

    def to_ocsf(self) -> dict[str, object]:
        """This record as an OCSF API Activity event."""
        if self.method == GRPC_METHOD:
            rpc_method = self.path.lstrip("/")
            activity, activity_name = ocsf.api_activity_for_rpc(rpc_method.rpartition("/")[2])
            call = ocsf.rpc_call_attributes(
                account_id=self.account_id,
                method=rpc_method,
                src_host=self.src_host,
                src_port=self.src_port,
            )
        else:
            activity, activity_name = ocsf.api_activity_for_method(self.method)
            call = ocsf.api_call_attributes(
                account_id=self.account_id,
                method=self.method,
                path=self.path,
                src_host=self.src_host,
                src_port=self.src_port,
            )
        status, level = _status_of(self.outcome)
        unmapped: dict[str, object] = {}
        if self.payload_hash is not None:
            unmapped["payload_hash"] = self.payload_hash
        return ocsf.event(
            ocsf.API_ACTIVITY,
            activity,
            activity_name=activity_name,
            time_ms=ocsf.epoch_ms(self.ts_utc),
            level=level,
            status=status,
            status_code=self.outcome,
            status_detail=self.detail,
            message=f"{self.method} {self.path}: {self.outcome}",
            metadata_attributes={"event_code": AUDIT_EVENT_CODE},
            attributes=call,
            unmapped=unmapped,
        )


def _row_to_record(row: sqlite3.Row) -> AuditRecord:
    return AuditRecord(
        ts_utc=datetime.fromisoformat(row["ts_utc"]),
        account_id=row["account_id"],
        method=row["method"],
        path=row["path"],
        payload_hash=row["payload_hash"],
        outcome=row["outcome"],
        detail=row["detail"],
        src_host=row["src_host"],
        src_port=row["src_port"],
    )


@dataclass(frozen=True, slots=True)
class AuditLog:
    db: BridgeDatabase

    def record(
        self,
        *,
        now: datetime,
        account_id: str | None,
        method: str,
        path: str,
        payload_hash: str | None,
        outcome: str,
        detail: str | None = None,
        client: tuple[str, int] | None = None,
    ) -> None:
        """``client`` is the request's peer ``(host, port)``, when known."""
        src_host, src_port = client if client is not None else (None, None)
        entry = AuditRecord(
            ts_utc=now,
            account_id=account_id,
            method=method,
            path=path,
            payload_hash=payload_hash,
            outcome=outcome,
            detail=detail,
            src_host=src_host,
            src_port=src_port,
        )
        with self.db.transaction() as conn:
            conn.execute(
                "INSERT INTO audit_log "
                "(ts_utc, account_id, method, path, payload_hash, outcome, detail, "
                "src_host, src_port) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    now.isoformat(),
                    account_id,
                    method,
                    path,
                    payload_hash,
                    outcome,
                    detail,
                    src_host,
                    src_port,
                ),
            )
        _LOG.info(
            "%s %s: %s", method, path, outcome, extra=ocsf.event_as_log_extra(entry.to_ocsf())
        )

    def list_no_transaction(self) -> tuple[AuditRecord, ...]:
        rows = self.db.fetchall("SELECT * FROM audit_log ORDER BY id")
        return tuple(_row_to_record(row) for row in rows)
