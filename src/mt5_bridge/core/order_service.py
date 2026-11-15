"""Orchestrates idempotent submit/cancel/replace/amend (plan §114, §56, §86).

The only place that calls the terminal for order lifecycle operations. Every
path ends in exactly one of two committed transactions: the journal's
initial ``RECEIVED`` insert, or a later ``record_result``-plus-sequence-log
append — never a partial write of one without the other (the "share a local
transaction" requirement). A lost response from the terminal always resolves
to ``UNKNOWN``, never a blind retry (§56).
"""

import asyncio
import hashlib
import json
from collections.abc import Callable
from dataclasses import dataclass
from datetime import timedelta
from decimal import Decimal
from time import perf_counter
from typing import TypeVar

from mt5_bridge.clock import Clock
from mt5_bridge.core.clock_drift import ClockDriftState, within_threshold
from mt5_bridge.core.database import BridgeDatabase
from mt5_bridge.core.emergency_actions import cancel_all_open_orders, flatten_positions
from mt5_bridge.core.execution_events import finalize_and_publish
from mt5_bridge.core.kill_switch import KillSwitch
from mt5_bridge.core.limits_guard import LimitsGuard, LimitsState
from mt5_bridge.core.metrics_port import Metrics
from mt5_bridge.core.oms_state import BridgeOrderState, ReplaceOutcome, map_terminal_state
from mt5_bridge.core.order_journal import JournalEntry, OrderJournal, ReceiveOutcome
from mt5_bridge.core.quote_policy import conservative_notional_price
from mt5_bridge.core.reconciliation import classify_unconfirmed, derive_tag
from mt5_bridge.core.replace_operations import ReceiveOutcome as ReplaceReceiveOutcome
from mt5_bridge.core.replace_operations import ReplaceOperationRow, ReplaceOperationStore
from mt5_bridge.core.sequence_log import SequenceLog
from mt5_bridge.core.terminal_watchdog import TerminalWatchdogState
from mt5_bridge.core.worker import (
    BridgeWorker,
    CommandPriority,
    WorkerQueueSaturated,
    WorkerStopped,
)
from mt5_bridge.mt5.client import Mt5Client
from mt5_bridge.mt5.errors import Mt5ConnectionLost
from mt5_bridge.mt5.models import (
    FillingMode,
    ModifyOrderRequest,
    Mt5Order,
    Mt5RetCode,
    NewOrderIntent,
    OrderSide,
    OrderType,
    TimeInForce,
)
from mt5_bridge.mt5.retcodes import UnrecognizedRetcode

T = TypeVar("T")

_FILLING_MODE_PREFERENCE = (FillingMode.RETURN, FillingMode.IOC, FillingMode.FOK)
_DEFAULT_RECONCILIATION_WINDOW = timedelta(hours=24)
_DEFAULT_QUOTE_MAX_AGE_SECONDS = 5.0


class IdempotencyConflict(Exception):
    def __init__(self, key: str) -> None:
        super().__init__(f"{key!r} was already used with a different payload")
        self.key = key


class OrderNotFound(Exception):
    def __init__(self, client_order_id: str) -> None:
        super().__init__(f"no journal entry for client_order_id {client_order_id!r}")
        self.client_order_id = client_order_id


class NotReconcilable(ValueError):
    """Only UNKNOWN or RECEIVED entries can be reconciled on demand."""


class OrderNotAmendable(ValueError):
    """Only an ACKNOWLEDGED entry with a ticket can be amended."""


class WorkerUnavailable(Exception):
    """A command was shed (``WorkerQueueSaturated``) or the worker stopped
    before dispatch (``WorkerStopped``) — it is certain the terminal never
    saw it, unlike ``Mt5ConnectionLost``, which happens mid-flight. Raised
    (never silently swallowed) for cancel/amend/replace's cancel leg, whose
    journal entry has an existing, unrelated state that must not be
    overwritten; safe to retry, since nothing changed. ``submit_order``
    instead finalizes its own fresh RECEIVED row to REJECTED directly,
    because that row must not be left dangling."""

    def __init__(self, cause: Exception) -> None:
        super().__init__(f"worker unavailable before reaching the terminal: {cause}")


_WORKER_UNAVAILABLE = (WorkerQueueSaturated, WorkerStopped)


class ClockDriftExceeded(Exception):
    """Raised before a cancel-replace even begins (no operation row is
    journaled): a transient condition, unlike a rejected ``client_order_id``,
    so the same request can simply be retried once drift clears."""

    def __init__(self, drift_seconds: float, threshold_seconds: float) -> None:
        super().__init__(
            f"clock drift {drift_seconds:.3f}s exceeds threshold {threshold_seconds:.3f}s"
        )
        self.drift_seconds = drift_seconds
        self.threshold_seconds = threshold_seconds


def select_filling_mode(available: frozenset[FillingMode]) -> FillingMode | None:
    """Pick a filling mode the symbol actually supports (never a constant).

    Preference order is a bridge convention (RETURN suits a resting-order
    exchange venue like B3 — plan §114), applied only to whatever the
    symbol's own ``filling_modes`` advertises.
    """
    for candidate in _FILLING_MODE_PREFERENCE:
        if candidate in available:
            return candidate
    return next(iter(available), None)


@dataclass(frozen=True, slots=True)
class SubmitResult:
    entry: JournalEntry
    replayed: bool


@dataclass(frozen=True, slots=True)
class ReplaceOrderSpec:
    symbol: str
    side: OrderSide
    order_type: OrderType
    volume: Decimal
    time_in_force: TimeInForce
    price: Decimal | None = None
    stop_loss: Decimal | None = None
    take_profit: Decimal | None = None
    expiration: int | None = None


@dataclass(frozen=True, slots=True)
class ReplaceResult:
    outcome: ReplaceOutcome
    original: JournalEntry | None
    new: JournalEntry | None


@dataclass(frozen=True, slots=True)
class CancelAllResult:
    attempted: tuple[int, ...]
    canceled: tuple[int, ...]
    failed: tuple[int, ...]
    journal_updated: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class FlattenResult:
    submitted: tuple[SubmitResult, ...]


def _hash_payload(*parts: object) -> str:
    canonical = json.dumps([str(part) for part in parts], sort_keys=False)
    return hashlib.sha256(canonical.encode()).hexdigest()


class OrderService:
    def __init__(
        self,
        *,
        db: BridgeDatabase,
        journal: OrderJournal,
        replace_operations: ReplaceOperationStore,
        sequence_log: SequenceLog,
        limits_guard: LimitsGuard,
        kill_switch: KillSwitch,
        worker: BridgeWorker,
        clock: Clock,
        magic: int,
        limits_state_provider: Callable[[], LimitsState],
        clock_drift_state: ClockDriftState,
        clock_drift_threshold_seconds: float,
        metrics: Metrics,
        reconciliation_window: timedelta = _DEFAULT_RECONCILIATION_WINDOW,
        quote_max_age_seconds: float = _DEFAULT_QUOTE_MAX_AGE_SECONDS,
        terminal_watchdog_state: TerminalWatchdogState | None = None,
    ) -> None:
        self._db = db
        self._journal = journal
        self._replace_operations = replace_operations
        self._sequence_log = sequence_log
        self._limits_guard = limits_guard
        self._kill_switch = kill_switch
        self._worker = worker
        self._clock = clock
        self._magic = magic
        self._limits_state_provider = limits_state_provider
        self._clock_drift_state = clock_drift_state
        self._clock_drift_threshold_seconds = clock_drift_threshold_seconds
        self._metrics = metrics
        self._reconciliation_window = reconciliation_window
        self._quote_max_age_seconds = quote_max_age_seconds
        self._terminal_watchdog_state = terminal_watchdog_state or TerminalWatchdogState()

    def _drift_within_threshold(self) -> bool:
        return within_threshold(
            self._clock_drift_state.drift_seconds, self._clock_drift_threshold_seconds
        )

    @property
    def worker(self) -> BridgeWorker:
        """The worker this service submits terminal commands through.

        Exposed so an embedder (or a test using ``BridgeWorker.drain()``
        instead of a real thread) can pump it.
        """
        return self._worker

    async def _run(self, priority: CommandPriority, command: Callable[[Mt5Client], T]) -> T:
        future = self._worker.submit(priority, command)
        return await asyncio.wrap_future(future)

    def _finalize(
        self,
        client_order_id: str,
        *,
        state: BridgeOrderState,
        ticket: int | None,
        mt5_retcode: int | None,
        mt5_last_error: str | None,
        reason: str | None,
    ) -> JournalEntry:
        return finalize_and_publish(
            db=self._db,
            journal=self._journal,
            sequence_log=self._sequence_log,
            client_order_id=client_order_id,
            state=state,
            ticket=ticket,
            mt5_retcode=mt5_retcode,
            mt5_last_error=mt5_last_error,
            reason=reason,
            now=self._clock.now_utc(),
        )

    def _reject_worker_unavailable(
        self, client_order_id: str, error: WorkerQueueSaturated | WorkerStopped
    ) -> SubmitResult:
        """A fresh RECEIVED row must not be left dangling: this command
        never reached the terminal (shed for capacity, or the worker
        stopped before dispatch), so REJECTED is certain, not a guess —
        unlike ``Mt5ConnectionLost``, which can happen mid-flight."""
        return SubmitResult(
            entry=self._finalize(
                client_order_id,
                state=BridgeOrderState.REJECTED,
                ticket=None,
                mt5_retcode=None,
                mt5_last_error=None,
                reason=f"worker unavailable before reaching the terminal: {error}",
            ),
            replayed=False,
        )

    async def _lookup_broker_order(self, ticket: int) -> Mt5Order | None:
        now = self._clock.now_utc()
        window_start = int((now - self._reconciliation_window).timestamp())
        now_epoch = int(now.timestamp())

        def query(client: Mt5Client) -> Mt5Order | None:
            for order in (*client.orders(), *client.history_orders(window_start, now_epoch)):
                if order.ticket == ticket:
                    return order
            return None

        return await self._run(CommandPriority.ORDER, query)

    async def _filled_volume(self, ticket: int) -> Decimal:
        """Sum the deals the terminal booked against ``ticket`` within the
        reconciliation window."""
        now = self._clock.now_utc()
        window_start = int((now - self._reconciliation_window).timestamp())
        now_epoch = int(now.timestamp())

        def query(client: Mt5Client) -> Decimal:
            deals = client.history_deals(window_start, now_epoch)
            return sum((d.volume for d in deals if d.order_ticket == ticket), Decimal(0))

        return await self._run(CommandPriority.ORDER, query)

    async def submit_order(
        self,
        *,
        client_order_id: str,
        symbol: str,
        side: OrderSide,
        order_type: OrderType,
        volume: Decimal,
        time_in_force: TimeInForce,
        price: Decimal | None = None,
        stop_loss: Decimal | None = None,
        take_profit: Decimal | None = None,
        expiration: int | None = None,
        emergency_reduction: bool = False,
    ) -> SubmitResult:
        """Times the whole submit attempt (plan §84 submit->ack latency) and
        classifies the resulting journal state into the rejection/UNKNOWN
        counters, around ``_submit_order_impl``'s many internal outcomes --
        a thin wrapper rather than instrumenting each of that method's
        branches individually."""
        started = perf_counter()
        result = await self._submit_order_impl(
            client_order_id=client_order_id,
            symbol=symbol,
            side=side,
            order_type=order_type,
            volume=volume,
            time_in_force=time_in_force,
            price=price,
            stop_loss=stop_loss,
            take_profit=take_profit,
            expiration=expiration,
            emergency_reduction=emergency_reduction,
        )
        self._metrics.record_duration(
            "mt5_bridge.order.submit_ack.duration", perf_counter() - started, symbol=symbol
        )
        if result.entry.state is BridgeOrderState.REJECTED:
            self._metrics.increment(
                "mt5_bridge.order.rejected", reason=result.entry.reason or "unknown", symbol=symbol
            )
        elif result.entry.state is BridgeOrderState.UNKNOWN:
            self._metrics.increment("mt5_bridge.order.unknown", symbol=symbol)
        return result

    async def _submit_order_impl(
        self,
        *,
        client_order_id: str,
        symbol: str,
        side: OrderSide,
        order_type: OrderType,
        volume: Decimal,
        time_in_force: TimeInForce,
        price: Decimal | None = None,
        stop_loss: Decimal | None = None,
        take_profit: Decimal | None = None,
        expiration: int | None = None,
        emergency_reduction: bool = False,
    ) -> SubmitResult:
        """``emergency_reduction`` exists for exactly one caller: ``flatten()``'s
        own closing orders (plan §107's ``EmergencyReductionPolicy``). The REST
        route never sets it (``SubmitOrderRequest`` has no such field), so an
        external client can never reach it. It:

        - bypasses the kill switch — without this, activating FLATTEN would
          immediately block the very reduction order FLATTEN exists to send,
          since ``blocks_new_orders()`` is true for every non-``NORMAL``
          kill-switch state;
        - tells ``LimitsGuard`` this order is reduce-only, so max-open-notional,
          max-daily-loss and stale-daily-loss-evidence never block it — those
          are exactly the conditions FLATTEN is likely to be reducing *because
          of*, and a reduction "capped at the reconciled position" cannot
          increase either figure by construction;
        - skips the fresh-quote requirement for an unpriced MARKET order: a
          missing or stale quote (e.g. a terminal disconnect) must not block a
          reduction either. A fresh quote, if one is available, is still used
          and persisted for exposure accounting; if none is available the
          order is recorded without a notional bound, since a reducing order
          only ever lowers exposure.

        The symbol allowlist, the per-symbol UNKNOWN block, clock drift, and
        idempotency all still apply — unchanged by this flag.
        """
        payload_hash = _hash_payload(
            symbol,
            side,
            order_type,
            volume,
            price,
            time_in_force,
            stop_loss,
            take_profit,
            expiration,
        )
        now = self._clock.now_utc()
        with self._db.transaction() as conn:
            outcome, entry = self._journal.begin_receive(
                conn,
                client_order_id=client_order_id,
                payload_hash=payload_hash,
                symbol=symbol,
                side=side,
                order_type=order_type,
                volume=volume,
                price=price,
                time_in_force=time_in_force,
                magic=self._magic,
                now=now,
            )
        if outcome is ReceiveOutcome.CONFLICT:
            raise IdempotencyConflict(client_order_id)
        if outcome is ReceiveOutcome.DUPLICATE_SAME_PAYLOAD:
            return SubmitResult(entry=entry, replayed=True)

        if not emergency_reduction and self._kill_switch.blocks_new_orders():
            return SubmitResult(
                entry=self._finalize(
                    client_order_id,
                    state=BridgeOrderState.REJECTED,
                    ticket=None,
                    mt5_retcode=None,
                    mt5_last_error=None,
                    reason="kill switch engaged",
                ),
                replayed=False,
            )

        if not self._terminal_watchdog_state.connected:
            # Never sends while the watchdog has detected a dropped terminal
            # (plan §114 UTM VM Operations) -- unconditionally, including
            # ``emergency_reduction``: a disconnected terminal cannot honor
            # any command regardless of intent.
            return SubmitResult(
                entry=self._finalize(
                    client_order_id,
                    state=BridgeOrderState.REJECTED,
                    ticket=None,
                    mt5_retcode=None,
                    mt5_last_error=None,
                    reason="terminal disconnected",
                ),
                replayed=False,
            )

        if not self._drift_within_threshold():
            return SubmitResult(
                entry=self._finalize(
                    client_order_id,
                    state=BridgeOrderState.REJECTED,
                    ticket=None,
                    mt5_retcode=None,
                    mt5_last_error=None,
                    reason=(
                        f"clock drift {self._clock_drift_state.drift_seconds:.3f}s exceeds "
                        f"threshold {self._clock_drift_threshold_seconds:.3f}s"
                    ),
                ),
                replayed=False,
            )

        if self._journal.has_unknown_for_symbol_no_transaction(symbol):
            return SubmitResult(
                entry=self._finalize(
                    client_order_id,
                    state=BridgeOrderState.REJECTED,
                    ticket=None,
                    mt5_retcode=None,
                    mt5_last_error=None,
                    reason=f"{symbol} has an unresolved UNKNOWN order; blocked until reconciled",
                ),
                replayed=False,
            )

        if price is not None:
            estimated_notional = volume * price
        elif order_type is OrderType.MARKET:
            # A MARKET order carries no price of its own, but it is the type
            # most likely to fill immediately: LimitsGuard must see a
            # conservative bound, not zero (audit F2), or reject outright
            # when no sufficiently fresh quote exists to bound it with.
            try:
                tick = await self._run(CommandPriority.ORDER, lambda c: c.symbol_tick(symbol))
            except _WORKER_UNAVAILABLE as error:
                return self._reject_worker_unavailable(client_order_id, error)
            quote_read_at = self._clock.now_utc()
            bound = conservative_notional_price(
                tick, side=side, now=quote_read_at, max_age_seconds=self._quote_max_age_seconds
            )
            if bound.price is None and not emergency_reduction:
                return SubmitResult(
                    entry=self._finalize(
                        client_order_id,
                        state=BridgeOrderState.REJECTED,
                        ticket=None,
                        mt5_retcode=None,
                        mt5_last_error=None,
                        reason=bound.rejection_reason,
                    ),
                    replayed=False,
                )
            if bound.price is not None:
                estimated_notional = volume * bound.price
                # Persisted now, on the RECEIVED row already inserted above,
                # so a restart before this order reaches a terminal state
                # still recovers its exposure (audit F2) — regardless of what
                # the guard decides next. Done for an emergency-reduction
                # order too, whenever a fresh quote happens to be available.
                with self._db.transaction() as conn:
                    self._journal.set_notional_price(
                        conn,
                        client_order_id=client_order_id,
                        notional_price=bound.price,
                        now=self._clock.now_utc(),
                    )
            else:
                # emergency_reduction with no valid quote: recorded without a
                # notional bound rather than guessed — a reducing order only
                # ever lowers exposure, so leaving it uncounted is safe.
                estimated_notional = Decimal(0)
        else:
            estimated_notional = Decimal(0)

        decision = self._limits_guard.check(
            symbol=symbol,
            volume=volume,
            estimated_notional=estimated_notional,
            state=self._limits_state_provider(),
            reduce_only=emergency_reduction,
        )
        if not decision.approved:
            return SubmitResult(
                entry=self._finalize(
                    client_order_id,
                    state=BridgeOrderState.REJECTED,
                    ticket=None,
                    mt5_retcode=None,
                    mt5_last_error=None,
                    reason="; ".join(decision.reasons),
                ),
                replayed=False,
            )

        try:
            symbol_info = await self._run(CommandPriority.ORDER, lambda c: c.symbol_info(symbol))
        except _WORKER_UNAVAILABLE as error:
            return self._reject_worker_unavailable(client_order_id, error)
        if symbol_info is None:
            return SubmitResult(
                entry=self._finalize(
                    client_order_id,
                    state=BridgeOrderState.REJECTED,
                    ticket=None,
                    mt5_retcode=None,
                    mt5_last_error=None,
                    reason="unknown symbol",
                ),
                replayed=False,
            )
        if symbol_info.is_continuous_series:
            return SubmitResult(
                entry=self._finalize(
                    client_order_id,
                    state=BridgeOrderState.REJECTED,
                    ticket=None,
                    mt5_retcode=None,
                    mt5_last_error=None,
                    reason="continuous series symbols are not tradable; use the dated contract",
                ),
                replayed=False,
            )
        if time_in_force not in symbol_info.time_in_force_modes:
            return SubmitResult(
                entry=self._finalize(
                    client_order_id,
                    state=BridgeOrderState.REJECTED,
                    ticket=None,
                    mt5_retcode=None,
                    mt5_last_error=None,
                    reason=f"time in force {time_in_force.value} not supported for {symbol}",
                ),
                replayed=False,
            )
        filling_mode = select_filling_mode(symbol_info.filling_modes)
        if filling_mode is None:
            return SubmitResult(
                entry=self._finalize(
                    client_order_id,
                    state=BridgeOrderState.REJECTED,
                    ticket=None,
                    mt5_retcode=None,
                    mt5_last_error=None,
                    reason="symbol advertises no supported filling mode",
                ),
                replayed=False,
            )

        intent = NewOrderIntent(
            symbol=symbol,
            side=side,
            order_type=order_type,
            volume=volume,
            price=price,
            stop_loss=stop_loss,
            take_profit=take_profit,
            time_in_force=time_in_force,
            filling_mode=filling_mode,
            expiration=expiration,
            magic=self._magic,
            comment=derive_tag(client_order_id),
        )

        try:
            check_result = await self._run(CommandPriority.ORDER, lambda c: c.order_check(intent))
        except UnrecognizedRetcode as error:
            # Nothing was sent: an unrecognized pre-trade code is a rejection.
            return SubmitResult(
                entry=self._finalize(
                    client_order_id,
                    state=BridgeOrderState.REJECTED,
                    ticket=None,
                    mt5_retcode=error.raw,
                    mt5_last_error=None,
                    reason=str(error),
                ),
                replayed=False,
            )
        except Mt5ConnectionLost:
            return SubmitResult(
                entry=self._finalize(
                    client_order_id,
                    state=BridgeOrderState.REJECTED,
                    ticket=None,
                    mt5_retcode=None,
                    mt5_last_error="connection lost",
                    reason="pre-trade check: terminal connection lost",
                ),
                replayed=False,
            )
        except _WORKER_UNAVAILABLE as error:
            return self._reject_worker_unavailable(client_order_id, error)
        if check_result.retcode not in (Mt5RetCode.DONE, Mt5RetCode.PLACED):
            return SubmitResult(
                entry=self._finalize(
                    client_order_id,
                    state=BridgeOrderState.REJECTED,
                    ticket=None,
                    mt5_retcode=int(check_result.retcode),
                    mt5_last_error=None,
                    reason=check_result.comment,
                ),
                replayed=False,
            )

        try:
            send_result = await self._run(CommandPriority.ORDER, lambda c: c.order_send(intent))
        except UnrecognizedRetcode as error:
            # The order may exist at the terminal: record UNKNOWN, never guess.
            return SubmitResult(
                entry=self._finalize(
                    client_order_id,
                    state=BridgeOrderState.UNKNOWN,
                    ticket=None,
                    mt5_retcode=error.raw,
                    mt5_last_error=None,
                    reason=str(error),
                ),
                replayed=False,
            )
        except Mt5ConnectionLost:
            return SubmitResult(
                entry=self._finalize(
                    client_order_id,
                    state=BridgeOrderState.UNKNOWN,
                    ticket=None,
                    mt5_retcode=None,
                    mt5_last_error="lost response calling order_send",
                    reason="lost response calling order_send",
                ),
                replayed=False,
            )
        except _WORKER_UNAVAILABLE as error:
            # Unlike Mt5ConnectionLost above: the command never even reached
            # the terminal, so there is no ambiguity to preserve as UNKNOWN.
            return self._reject_worker_unavailable(client_order_id, error)

        if send_result.retcode is Mt5RetCode.DONE:
            state = (
                BridgeOrderState.FILLED
                if order_type is OrderType.MARKET
                else BridgeOrderState.ACKNOWLEDGED
            )
        elif send_result.retcode is Mt5RetCode.PLACED:
            state = BridgeOrderState.ACKNOWLEDGED
        else:
            state = BridgeOrderState.REJECTED
        entry = self._finalize(
            client_order_id,
            state=state,
            ticket=send_result.order,
            mt5_retcode=int(send_result.retcode),
            mt5_last_error=None,
            reason=send_result.comment,
        )
        return SubmitResult(entry=entry, replayed=False)

    async def reconcile_order(self, client_order_id: str) -> JournalEntry:
        """Re-examine one UNKNOWN/RECEIVED entry against the terminal.

        Runs ``reconciliation.classify_unconfirmed`` over the reconciliation
        window; an unresolved entry stays UNKNOWN and keeps its symbol blocked.
        """
        entry = self._journal.get_no_transaction(client_order_id)
        if entry is None:
            raise OrderNotFound(client_order_id)
        if entry.state not in (BridgeOrderState.UNKNOWN, BridgeOrderState.RECEIVED):
            raise NotReconcilable(f"{client_order_id!r} is {entry.state.value}, not UNKNOWN")
        now = self._clock.now_utc()
        start = now - self._reconciliation_window
        start_epoch, now_epoch = int(start.timestamp()), int(now.timestamp())
        try:
            open_orders = await self._run(CommandPriority.ORDER, lambda c: c.orders())
            history = await self._run(
                CommandPriority.ORDER, lambda c: c.history_orders(start_epoch, now_epoch)
            )
            deals = await self._run(
                CommandPriority.ORDER, lambda c: c.history_deals(start_epoch, now_epoch)
            )
        except _WORKER_UNAVAILABLE as error:
            raise WorkerUnavailable(error) from error
        state, ticket, reason = classify_unconfirmed(
            entry, broker_orders=[*open_orders, *history], deals=deals, window_start=start
        )
        return self._finalize(
            client_order_id,
            state=state,
            ticket=ticket,
            mt5_retcode=entry.mt5_retcode,
            mt5_last_error=entry.mt5_last_error,
            reason=reason,
        )

    async def cancel_order(self, client_order_id: str) -> JournalEntry:
        entry = self._journal.get_no_transaction(client_order_id)
        if entry is None:
            raise OrderNotFound(client_order_id)
        if entry.state in (
            BridgeOrderState.FILLED,
            BridgeOrderState.REJECTED,
            BridgeOrderState.CANCELED,
            BridgeOrderState.EXPIRED,
        ):
            return entry
        if entry.ticket is None:
            return self._finalize(
                client_order_id,
                state=BridgeOrderState.UNKNOWN,
                ticket=None,
                mt5_retcode=None,
                mt5_last_error=None,
                reason="cancel requested with no known terminal ticket",
            )

        try:
            result = await self._run(CommandPriority.ORDER, lambda c: c.cancel_order(entry.ticket))  # type: ignore[arg-type]
        except UnrecognizedRetcode as error:
            broker_order = await self._lookup_broker_order(entry.ticket)
            return self._finalize(
                client_order_id,
                state=(
                    map_terminal_state(broker_order.state)
                    if broker_order is not None
                    else BridgeOrderState.UNKNOWN
                ),
                ticket=entry.ticket,
                mt5_retcode=error.raw,
                mt5_last_error=None,
                reason=str(error),
            )
        except Mt5ConnectionLost:
            return self._finalize(
                client_order_id,
                state=BridgeOrderState.UNKNOWN,
                ticket=entry.ticket,
                mt5_retcode=None,
                mt5_last_error="lost response calling cancel_order",
                reason="lost response calling cancel_order",
            )
        except _WORKER_UNAVAILABLE as error:
            # entry.state is unchanged (the cancel never reached the
            # terminal) — safe to retry, so this is raised, not finalized.
            raise WorkerUnavailable(error) from error

        if result.retcode is Mt5RetCode.DONE:
            return self._finalize(
                client_order_id,
                state=BridgeOrderState.CANCELED,
                ticket=entry.ticket,
                mt5_retcode=int(result.retcode),
                mt5_last_error=None,
                reason=result.comment,
            )

        broker_order = await self._lookup_broker_order(entry.ticket)
        state = (
            map_terminal_state(broker_order.state)
            if broker_order is not None
            else BridgeOrderState.UNKNOWN
        )
        return self._finalize(
            client_order_id,
            state=state,
            ticket=entry.ticket,
            mt5_retcode=int(result.retcode),
            mt5_last_error=None,
            reason=result.comment,
        )

    async def amend_order(
        self,
        client_order_id: str,
        *,
        price: Decimal | None = None,
        stop_loss: Decimal | None = None,
        take_profit: Decimal | None = None,
        expiration: int | None = None,
    ) -> JournalEntry:
        entry = self._journal.get_no_transaction(client_order_id)
        if entry is None:
            raise OrderNotFound(client_order_id)
        if entry.ticket is None or entry.state is not BridgeOrderState.ACKNOWLEDGED:
            raise OrderNotAmendable(
                f"{client_order_id!r} is not in an amendable state ({entry.state})"
            )

        try:
            result = await self._run(
                CommandPriority.ORDER,
                lambda c: c.modify_order(
                    ModifyOrderRequest(
                        ticket=entry.ticket,  # type: ignore[arg-type]
                        price=price,
                        stop_loss=stop_loss,
                        take_profit=take_profit,
                        expiration=expiration,
                    )
                ),
            )
        except UnrecognizedRetcode as error:
            return self._finalize(
                client_order_id,
                state=BridgeOrderState.UNKNOWN,
                ticket=entry.ticket,
                mt5_retcode=error.raw,
                mt5_last_error=None,
                reason=str(error),
            )
        except Mt5ConnectionLost:
            return self._finalize(
                client_order_id,
                state=BridgeOrderState.UNKNOWN,
                ticket=entry.ticket,
                mt5_retcode=None,
                mt5_last_error="lost response calling modify_order",
                reason="lost response calling modify_order",
            )
        except _WORKER_UNAVAILABLE as error:
            raise WorkerUnavailable(error) from error

        new_state = (
            entry.state if result.retcode is Mt5RetCode.DONE else BridgeOrderState.ACKNOWLEDGED
        )
        return self._finalize(
            client_order_id,
            state=new_state,
            ticket=entry.ticket,
            mt5_retcode=int(result.retcode),
            mt5_last_error=None,
            reason=result.comment,
        )

    async def replace_order(
        self,
        *,
        operation_id: str,
        original_client_order_id: str,
        new_client_order_id: str,
        new_order: ReplaceOrderSpec,
    ) -> ReplaceResult:
        if not self._drift_within_threshold():
            raise ClockDriftExceeded(
                self._clock_drift_state.drift_seconds, self._clock_drift_threshold_seconds
            )

        payload_hash = _hash_payload(
            original_client_order_id,
            new_client_order_id,
            new_order.symbol,
            new_order.side,
            new_order.order_type,
            new_order.volume,
            new_order.price,
            new_order.time_in_force,
            new_order.stop_loss,
            new_order.take_profit,
            new_order.expiration,
        )
        now = self._clock.now_utc()
        with self._db.transaction() as conn:
            receive_outcome, operation = self._replace_operations.begin_receive(
                conn,
                operation_id=operation_id,
                original_client_order_id=original_client_order_id,
                new_client_order_id=new_client_order_id,
                payload_hash=payload_hash,
                now=now,
            )
        if receive_outcome is ReplaceReceiveOutcome.CONFLICT:
            raise IdempotencyConflict(operation_id)
        if receive_outcome is ReplaceReceiveOutcome.DUPLICATE_SAME_PAYLOAD:
            return self._replace_result_from_row(operation)

        original = self._journal.get_no_transaction(original_client_order_id)
        if original is None or original.ticket is None:
            raise OrderNotFound(original_client_order_id)

        try:
            cancel_result = await self._run(
                CommandPriority.ORDER,
                lambda c: c.cancel_order(original.ticket),  # type: ignore[arg-type]
            )
        except UnrecognizedRetcode as error:
            self._finalize(
                original_client_order_id,
                state=BridgeOrderState.UNKNOWN,
                ticket=original.ticket,
                mt5_retcode=error.raw,
                mt5_last_error=None,
                reason=str(error),
            )
            return self._set_operation_outcome(
                operation_id, ReplaceOutcome.UNKNOWN, original_client_order_id, None
            )
        except Mt5ConnectionLost:
            self._finalize(
                original_client_order_id,
                state=BridgeOrderState.UNKNOWN,
                ticket=original.ticket,
                mt5_retcode=None,
                mt5_last_error="lost response canceling the original leg",
                reason="lost response canceling the original leg",
            )
            return self._set_operation_outcome(
                operation_id, ReplaceOutcome.UNKNOWN, original_client_order_id, None
            )
        except _WORKER_UNAVAILABLE:
            # The cancel never reached the terminal: the original leg's own
            # state is left untouched (no ambiguity to resolve, unlike
            # Mt5ConnectionLost above), and only the replace operation
            # itself resolves — CANCEL_FAILED, matching "nothing changed" —
            # so a same-operation_id replay never sees a dangling PENDING
            # row (never a valid outcome to hand back, per ReplaceOutcome's
            # own docstring).
            return self._set_operation_outcome(
                operation_id, ReplaceOutcome.CANCEL_FAILED, original_client_order_id, None
            )

        if cancel_result.retcode is not Mt5RetCode.DONE:
            broker_order = await self._lookup_broker_order(original.ticket)
            state = (
                map_terminal_state(broker_order.state)
                if broker_order is not None
                else BridgeOrderState.UNKNOWN
            )
            self._finalize(
                original_client_order_id,
                state=state,
                ticket=original.ticket,
                mt5_retcode=int(cancel_result.retcode),
                mt5_last_error=None,
                reason=cancel_result.comment,
            )
            return self._set_operation_outcome(
                operation_id, ReplaceOutcome.CANCEL_FAILED, original_client_order_id, None
            )

        self._finalize(
            original_client_order_id,
            state=BridgeOrderState.CANCELED,
            ticket=original.ticket,
            mt5_retcode=int(cancel_result.retcode),
            mt5_last_error=None,
            reason="canceled for replace",
        )
        with self._db.transaction() as conn:
            self._journal.link_replacement(
                conn,
                original_client_order_id=original_client_order_id,
                new_client_order_id=new_client_order_id,
                now=self._clock.now_utc(),
            )

        # §114/§56 cancel-replace remainder: the new leg only ever asks for
        # what is still needed to reach the request's desired total — never
        # the full ``new_order.volume`` again, which would double the fill.
        # What already filled is read from the original ticket's deals: the
        # terminal's cancel result carries no dealt volume (075 demo
        # evidence), so it says nothing about fills before the cancel.
        # Unreadable fills or nothing left to buy/sell leave only the
        # cancel in place — never a guessed or zero-volume new leg.
        try:
            remaining_volume = new_order.volume - await self._filled_volume(original.ticket)
        except (Mt5ConnectionLost, *_WORKER_UNAVAILABLE):
            remaining_volume = None
        if remaining_volume is None or remaining_volume <= 0:
            return self._set_operation_outcome(
                operation_id,
                ReplaceOutcome.CANCELED_NEW_REJECTED,
                original_client_order_id,
                None,
            )

        submit_result = await self.submit_order(
            client_order_id=new_client_order_id,
            symbol=new_order.symbol,
            side=new_order.side,
            order_type=new_order.order_type,
            volume=remaining_volume,
            time_in_force=new_order.time_in_force,
            price=new_order.price,
            stop_loss=new_order.stop_loss,
            take_profit=new_order.take_profit,
            expiration=new_order.expiration,
        )
        outcome = (
            ReplaceOutcome.REPLACED
            if submit_result.entry.state
            in (
                BridgeOrderState.ACKNOWLEDGED,
                BridgeOrderState.FILLED,
                BridgeOrderState.PARTIALLY_FILLED,
            )
            else ReplaceOutcome.CANCELED_NEW_REJECTED
        )
        return self._set_operation_outcome(
            operation_id, outcome, original_client_order_id, new_client_order_id
        )

    def _set_operation_outcome(
        self,
        operation_id: str,
        outcome: ReplaceOutcome,
        original_client_order_id: str,
        new_client_order_id: str | None,
    ) -> ReplaceResult:
        now = self._clock.now_utc()
        with self._db.transaction() as conn:
            self._replace_operations.update_outcome(
                conn, operation_id=operation_id, outcome=outcome, now=now
            )
        return ReplaceResult(
            outcome=outcome,
            original=self._journal.get_no_transaction(original_client_order_id),
            new=self._journal.get_no_transaction(new_client_order_id)
            if new_client_order_id
            else None,
        )

    def _replace_result_from_row(self, operation: ReplaceOperationRow) -> ReplaceResult:
        return ReplaceResult(
            outcome=operation.outcome,
            original=self._journal.get_no_transaction(operation.original_client_order_id),
            new=self._journal.get_no_transaction(operation.new_client_order_id),
        )

    async def cancel_all(self) -> CancelAllResult:
        """Kill-switch CANCEL_ALL and the dead-man sweep both drive this
        (plan §107): cancel every open order at the terminal, then reconcile
        the journal so a canceled ticket is never left stuck ACKNOWLEDGED.
        Mirrors ``cancel_all_open_orders``'s own contract — a per-order
        failure is reported, never raised, so one bad order cannot stop the
        rest of the sweep.
        """
        try:
            report = await self._run(CommandPriority.ORDER, cancel_all_open_orders)
        except _WORKER_UNAVAILABLE as error:
            raise WorkerUnavailable(error) from error
        updated: list[str] = []
        canceled_tickets = set(report.canceled)
        if canceled_tickets:
            for entry in self._journal.list_open_no_transaction():
                if entry.ticket in canceled_tickets:
                    self._finalize(
                        entry.client_order_id,
                        state=BridgeOrderState.CANCELED,
                        ticket=entry.ticket,
                        mt5_retcode=None,
                        mt5_last_error=None,
                        reason="canceled by kill switch CANCEL_ALL",
                    )
                    updated.append(entry.client_order_id)
        return CancelAllResult(
            attempted=report.attempted,
            canceled=report.canceled,
            failed=report.failed,
            journal_updated=tuple(updated),
        )

    async def flatten(self, *, comment: str = "kill-switch flatten") -> FlattenResult:
        """Kill-switch FLATTEN (plan §107 EmergencyReductionPolicy): one
        closing MARKET intent per open position, capped exactly at the
        reconciled position (never rounded up, never guessed — see
        ``emergency_actions.flatten_positions``), submitted through the
        normal journaled ``submit_order`` path for auditability. Never
        promises flat by construction alone: the caller reads each
        ``SubmitResult`` for the terminal's actual confirmation. Submitted
        with ``emergency_reduction=True``: FLATTEN itself just set the switch
        to a non-``NORMAL`` state, and neither that nor an exhausted/stale
        limit or a missing quote may block the very reduction order it
        exists to send (plan §107).
        """
        try:
            intents = await self._run(
                CommandPriority.ORDER,
                lambda c: flatten_positions(c, magic=self._magic, comment=comment),
            )
        except _WORKER_UNAVAILABLE as error:
            raise WorkerUnavailable(error) from error
        now = self._clock.now_utc()
        results: list[SubmitResult] = []
        for index, intent in enumerate(intents):
            result = await self.submit_order(
                client_order_id=f"flatten-{intent.symbol}-{now.timestamp():.6f}-{index}",
                symbol=intent.symbol,
                side=intent.side,
                order_type=intent.order_type,
                volume=intent.volume,
                time_in_force=intent.time_in_force,
                emergency_reduction=True,
            )
            results.append(result)
        return FlattenResult(submitted=tuple(results))
