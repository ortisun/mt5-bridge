"""Turn snapshot/history differences into events (plan §114).

Without terminal push support, the bridge polls and diffs. ``TickPoller``
and ``BarPoller`` keep an in-memory-only cursor (ticks/bars are not
replayable, so nothing is lost by starting fresh after a restart —
reconnecting clients reload via REST). ``BarPoller`` never trusts a raw
client to have already excluded the forming bar: it both requests
``start_pos=1`` *and* filters on ``is_closed`` (plan rule 28). ``BookPoller``
has no source timestamp to compare against (MT5 depth carries none), so it
publishes on a changed snapshot rather than a newer one, and — like ticks and
bars — is not replayable: a gap means the client reloads via REST.
``ExecutionPoller`` detects fills/closures between polls via
``history_orders_get`` with a *persisted*, overlapping cursor — "open-order
snapshot differences are insufficient" — and publishes through the same
``finalize_and_publish`` path as everything else that changes journal state.
"""

from collections.abc import Iterable
from dataclasses import dataclass

from mt5_bridge.clock import Clock
from mt5_bridge.core.database import BridgeDatabase
from mt5_bridge.core.execution_events import finalize_and_publish
from mt5_bridge.core.oms_state import map_terminal_state
from mt5_bridge.core.order_journal import OrderJournal
from mt5_bridge.core.sequence_log import SequenceLog
from mt5_bridge.mt5.client import Mt5Client
from mt5_bridge.mt5.models import Bar, BookLevel, Tick, Timeframe


class TickPoller:
    def __init__(self) -> None:
        self._last_time_msc: dict[str, int] = {}

    def poll_once(self, client: Mt5Client, symbols: Iterable[str]) -> tuple[Tick, ...]:
        events: list[Tick] = []
        for symbol in symbols:
            tick = client.symbol_tick(symbol)
            if tick is None:
                continue
            last = self._last_time_msc.get(symbol)
            if last is not None and tick.time_msc <= last:
                continue
            self._last_time_msc[symbol] = tick.time_msc
            events.append(tick)
        return tuple(events)


class BarPoller:
    def __init__(self, *, lookback: int = 5) -> None:
        self._lookback = lookback
        self._last_closed_time: dict[tuple[str, Timeframe], int] = {}

    def poll_once(
        self, client: Mt5Client, subscriptions: Iterable[tuple[str, Timeframe]]
    ) -> tuple[Bar, ...]:
        events: list[Bar] = []
        for symbol, timeframe in subscriptions:
            # start_pos=1 already asks the client to skip the forming bar;
            # `bar.is_closed` below is the defensive second check (rule 28).
            bars = client.copy_bars(symbol, timeframe, start_pos=1, count=self._lookback)
            key = (symbol, timeframe)
            last = self._last_closed_time.get(key)
            for bar in bars:
                if not bar.is_closed:
                    continue
                if last is not None and bar.time <= last:
                    continue
                events.append(bar)
                last = bar.time
            if last is not None:
                self._last_closed_time[key] = last
        return tuple(events)


class BookPoller:
    """Polls one depth snapshot per subscribed symbol; publishes on change.

    A symbol the broker gives no depth for (``order_book`` returns ``None``)
    never produces an event — not even an empty one.
    """

    def __init__(self) -> None:
        self._last_levels: dict[str, tuple[BookLevel, ...]] = {}

    def poll_once(
        self, client: Mt5Client, symbols: Iterable[str]
    ) -> tuple[tuple[str, tuple[BookLevel, ...]], ...]:
        events: list[tuple[str, tuple[BookLevel, ...]]] = []
        for symbol in symbols:
            levels = client.order_book(symbol)
            if levels is None:
                continue
            if levels == self._last_levels.get(symbol):
                continue
            self._last_levels[symbol] = levels
            events.append((symbol, levels))
        return tuple(events)


@dataclass(frozen=True, slots=True)
class ExecutionPoller:
    """Detects fills/closures for open journal entries via order history.

    The cursor is persisted in the same SQLite database as the journal, so a
    bridge restart resumes from where it left off rather than re-scanning
    (or missing) history; ``overlap_seconds`` re-examines a trailing slice of
    already-covered time in case an update landed at the very edge of the
    previous window.
    """

    db: BridgeDatabase
    journal: OrderJournal
    sequence_log: SequenceLog
    overlap_seconds: int = 60
    stream_name: str = "executions"

    def __post_init__(self) -> None:
        with self.db.transaction() as conn:
            conn.execute(
                "INSERT OR IGNORE INTO poller_cursors (name, cursor_epoch) VALUES (?, 0)",
                (self.stream_name,),
            )

    def cursor(self) -> int:
        row = self.db.fetchone(
            "SELECT cursor_epoch FROM poller_cursors WHERE name = ?", (self.stream_name,)
        )
        return row["cursor_epoch"] if row is not None else 0

    def _advance_cursor(self, new_cursor: int) -> None:
        with self.db.transaction() as conn:
            conn.execute(
                "UPDATE poller_cursors SET cursor_epoch = ? WHERE name = ?",
                (new_cursor, self.stream_name),
            )

    def poll_once(self, client: Mt5Client, clock: Clock) -> tuple[str, ...]:
        now = clock.now_utc()
        now_epoch = int(now.timestamp())
        open_entries = {
            entry.ticket: entry
            for entry in self.journal.list_open_no_transaction()
            if entry.ticket is not None
        }
        if not open_entries:
            self._advance_cursor(now_epoch)
            return ()

        window_start = max(0, self.cursor() - self.overlap_seconds)
        history = client.history_orders(window_start, now_epoch)

        affected: list[str] = []
        for order in history:
            entry = open_entries.get(order.ticket)
            if entry is None:
                continue
            new_state = map_terminal_state(order.state)
            if new_state == entry.state:
                continue
            finalize_and_publish(
                db=self.db,
                journal=self.journal,
                sequence_log=self.sequence_log,
                client_order_id=entry.client_order_id,
                state=new_state,
                ticket=order.ticket,
                mt5_retcode=None,
                mt5_last_error=None,
                reason="detected by execution poller",
                now=now,
            )
            affected.append(entry.client_order_id)

        self._advance_cursor(now_epoch)
        return tuple(affected)
