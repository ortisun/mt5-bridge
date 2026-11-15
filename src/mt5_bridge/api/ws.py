"""``/v1/stream``: one WebSocket endpoint, subscription-based.

Plan §114 WebSocket v1. ``executions`` is durable and resumable through
``SequenceLog``; ``ticks``/``bars``/``book`` are not (a gap means the client
reloads through REST). A plain ``subscribe`` to ``executions`` and an
explicit ``resume`` share one mechanism and differ only in the starting
cursor: subscribing starts at ``from_seq=1`` (replay everything retained),
while ``resume`` starts at the caller's own ``from_seq`` — subject to
``RESYNC_REQUIRED`` if that cursor fell outside the retained window. A
single loop alternates between reading a client message (bounded by
``poll_interval``) and polling due market/execution data, so one connection
needs no extra background task.
"""

import asyncio
import json
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any

from fastapi import WebSocket
from starlette.websockets import WebSocketDisconnect

from mt5_bridge.api.run_on_worker import run_on_worker
from mt5_bridge.api.telemetry import stream_operation_span
from mt5_bridge.clock import Clock
from mt5_bridge.core.daily_loss_baseline import TradingDayPolicy
from mt5_bridge.core.envelope import StreamEnvelope, to_wire
from mt5_bridge.core.kill_switch import KillSwitch
from mt5_bridge.core.pollers import BarPoller, BookPoller, TickPoller
from mt5_bridge.core.sequence_log import ResumeStatus, SequenceEvent, SequenceLog
from mt5_bridge.core.terminal_watchdog import TerminalWatchdogState
from mt5_bridge.core.time_normalizer import to_utc
from mt5_bridge.core.trading_phase import classify_trading_phase
from mt5_bridge.core.worker import BridgeWorker, CommandPriority
from mt5_bridge.mt5.client import Mt5Client
from mt5_bridge.mt5.models import Bar, BookLevel, SourceTimeUnit, Tick, TimeBasis, Timeframe

DEFAULT_POLL_INTERVAL_SECONDS = 0.05
# RFC 6455 §7.4.1: 1008, a message that violates the endpoint's policy; here,
# one that is not JSON or does not match ws_messages.schema.json.
INVALID_MESSAGE_CLOSE_CODE = 1008
_INVALID_CLIENT_MESSAGE = (ValueError, KeyError, TypeError, AttributeError)


def _empty_symbols() -> set[str]:
    return set()


def _empty_bar_subscriptions() -> set[tuple[str, Timeframe]]:
    return set()


def _empty_local_seq() -> dict[str, int]:
    return {}


def _empty_phase_map() -> dict[str, str]:
    return {}


@dataclass
class _ConnectionState:
    tick_poller: TickPoller = field(default_factory=TickPoller)
    tick_symbols: set[str] = field(default_factory=_empty_symbols)
    bar_poller: BarPoller = field(default_factory=BarPoller)
    bar_symbols: set[tuple[str, Timeframe]] = field(default_factory=_empty_bar_subscriptions)
    book_poller: BookPoller = field(default_factory=BookPoller)
    book_symbols: set[str] = field(default_factory=_empty_symbols)
    executions_subscribed: bool = False
    executions_next_seq: int = 1
    status_subscribed: bool = False
    last_status_sent: tuple[str, bool] | None = None
    last_phase_sent: dict[str, str] = field(default_factory=_empty_phase_map)
    account_subscribed: bool = False
    last_account_sent: tuple[str, str, str, str] | None = None
    local_seq: dict[str, int] = field(default_factory=_empty_local_seq)

    def next_local_seq(self, stream_id: str) -> int:
        current = self.local_seq.get(stream_id, 0) + 1
        self.local_seq[stream_id] = current
        return current


def _tick_envelope(tick: Tick, *, seq: int, clock: Clock) -> dict[str, object]:
    # source_time_msc is set only by a wall-clock decorator (mt5/terminal_time.py)
    # to the epoch the terminal actually reported; fall back to time_msc when no
    # such decorator ran, so source_time_raw never shows the converted value.
    raw_time_msc = tick.time_msc if tick.source_time_msc is None else tick.source_time_msc
    envelope = StreamEnvelope(
        channel="ticks",
        stream_id=f"ticks:{tick.symbol}",
        seq=seq,
        ts_utc=to_utc(tick.time_msc, SourceTimeUnit.MILLISECONDS),
        source_time_raw=raw_time_msc,
        source_time_unit=SourceTimeUnit.MILLISECONDS,
        time_basis=TimeBasis.EXCHANGE_REPORTED,
        received_at=clock.now_utc(),
        payload={
            "symbol": tick.symbol,
            "bid": str(tick.bid),
            "ask": str(tick.ask),
            "last": str(tick.last),
            "volume": str(tick.volume),
        },
    )
    return to_wire(envelope)


def _bar_envelope(bar: Bar, *, seq: int, clock: Clock) -> dict[str, object]:
    # source_time is set only by a wall-clock decorator (mt5/terminal_time.py)
    # to the epoch the terminal actually reported; fall back to time when no
    # such decorator ran, so source_time_raw never shows the converted value.
    raw_time = bar.time if bar.source_time is None else bar.source_time
    envelope = StreamEnvelope(
        channel="bars",
        stream_id=f"bars:{bar.symbol}:{bar.timeframe.value}",
        seq=seq,
        ts_utc=to_utc(bar.time, SourceTimeUnit.SECONDS),
        source_time_raw=raw_time,
        source_time_unit=SourceTimeUnit.SECONDS,
        time_basis=TimeBasis.EXCHANGE_REPORTED,
        received_at=clock.now_utc(),
        payload={
            "symbol": bar.symbol,
            "timeframe": bar.timeframe.value,
            "open": str(bar.open),
            "high": str(bar.high),
            "low": str(bar.low),
            "close": str(bar.close),
            "volume": str(bar.volume),
        },
    )
    return to_wire(envelope)


def _book_envelope(
    symbol: str, levels: tuple[BookLevel, ...], *, seq: int, clock: Clock
) -> dict[str, object]:
    """MT5 depth carries no source timestamp (plan §114): ``ts_utc`` is the
    instant the bridge observed the snapshot, never an exchange time."""
    now = clock.now_utc()
    envelope = StreamEnvelope(
        channel="book",
        stream_id=f"book:{symbol}",
        seq=seq,
        ts_utc=now,
        source_time_raw=int(now.timestamp() * 1000),
        source_time_unit=SourceTimeUnit.MILLISECONDS,
        time_basis=TimeBasis.BRIDGE_OBSERVED,
        received_at=now,
        payload={
            "symbol": symbol,
            "levels": [
                {"side": level.side.value, "price": str(level.price), "volume": str(level.volume)}
                for level in levels
            ],
        },
    )
    return to_wire(envelope)


def _execution_envelope(event: SequenceEvent) -> dict[str, object]:
    """Reconstructed from a durable ``SequenceEvent``: an execution event's
    ``ts_utc`` already *is* the bridge-observed instant it was recorded at
    (plan §114: it is bridge-local bookkeeping, not an exchange timestamp)."""
    envelope = StreamEnvelope(
        channel="executions",
        stream_id="executions",
        seq=event.seq,
        ts_utc=event.ts_utc,
        source_time_raw=int(event.ts_utc.timestamp() * 1000),
        source_time_unit=SourceTimeUnit.MILLISECONDS,
        time_basis=TimeBasis.BRIDGE_OBSERVED,
        received_at=event.ts_utc,
        payload=json.loads(event.payload_json),
    )
    return to_wire(envelope)


def _status_envelope(
    *,
    kill_switch_state: str,
    terminal_connected: bool,
    symbol: str | None,
    trading_phase: str | None,
    seq: int,
    clock: Clock,
) -> dict[str, object]:
    now = clock.now_utc()
    payload: dict[str, object] = {
        "kill_switch_state": kill_switch_state,
        "terminal_connected": terminal_connected,
    }
    if symbol is not None:
        payload["symbol"] = symbol
        payload["trading_phase"] = trading_phase
    envelope = StreamEnvelope(
        channel="status",
        stream_id="status",
        seq=seq,
        ts_utc=now,
        source_time_raw=int(now.timestamp() * 1000),
        source_time_unit=SourceTimeUnit.MILLISECONDS,
        time_basis=TimeBasis.BRIDGE_OBSERVED,
        received_at=now,
        payload=payload,
    )
    return to_wire(envelope)


def _account_envelope(
    *, balance: str, equity: str, margin: str, margin_free: str, seq: int, clock: Clock
) -> dict[str, object]:
    now = clock.now_utc()
    envelope = StreamEnvelope(
        channel="account",
        stream_id="account",
        seq=seq,
        ts_utc=now,
        source_time_raw=int(now.timestamp() * 1000),
        source_time_unit=SourceTimeUnit.MILLISECONDS,
        time_basis=TimeBasis.BRIDGE_OBSERVED,
        received_at=now,
        payload={
            "balance": balance,
            "equity": equity,
            "margin": margin,
            "margin_free": margin_free,
        },
    )
    return to_wire(envelope)


async def _handle_client_message(
    websocket: WebSocket, message: dict[str, Any], state: _ConnectionState
) -> None:
    op = message.get("op")
    if op == "ping":
        await websocket.send_json({"type": "pong"})
        return
    channel = message.get("channel")
    if op == "subscribe":
        if channel == "ticks":
            state.tick_symbols.update(message.get("symbols", ()))
        elif channel == "bars":
            timeframe = Timeframe(message["timeframe"])
            state.bar_symbols.update((symbol, timeframe) for symbol in message.get("symbols", ()))
        elif channel == "book":
            state.book_symbols.update(message.get("symbols", ()))
        elif channel == "executions":
            state.executions_subscribed = True
        elif channel == "status":
            state.status_subscribed = True
        elif channel == "account":
            state.account_subscribed = True
        return
    if op == "unsubscribe":
        if channel == "ticks":
            state.tick_symbols.difference_update(message.get("symbols", ()))
        elif channel == "bars" and "timeframe" in message:
            timeframe = Timeframe(message["timeframe"])
            state.bar_symbols.difference_update(
                (symbol, timeframe) for symbol in message.get("symbols", ())
            )
        elif channel == "book":
            state.book_symbols.difference_update(message.get("symbols", ()))
        elif channel == "executions":
            state.executions_subscribed = False
        elif channel == "status":
            state.status_subscribed = False
        elif channel == "account":
            state.account_subscribed = False
        return
    if op == "resume" and channel == "executions":
        state.executions_subscribed = True
        from_seq = int(message["from_seq"])
        if from_seq < 1:
            raise ValueError("from_seq must be >= 1")
        state.executions_next_seq = from_seq
        return


async def _publish_market_data(
    websocket: WebSocket, state: _ConnectionState, *, worker: BridgeWorker, clock: Clock
) -> None:
    if state.tick_symbols:
        symbols = tuple(state.tick_symbols)
        ticks = await run_on_worker(
            worker, CommandPriority.MARKET_POLL, lambda c: state.tick_poller.poll_once(c, symbols)
        )
        for tick in ticks:
            stream_id = f"ticks:{tick.symbol}"
            await websocket.send_json(
                _tick_envelope(tick, seq=state.next_local_seq(stream_id), clock=clock)
            )

    if state.bar_symbols:
        subscriptions = tuple(state.bar_symbols)
        bars = await run_on_worker(
            worker,
            CommandPriority.MARKET_POLL,
            lambda c: state.bar_poller.poll_once(c, subscriptions),
        )
        for bar in bars:
            stream_id = f"bars:{bar.symbol}:{bar.timeframe.value}"
            await websocket.send_json(
                _bar_envelope(bar, seq=state.next_local_seq(stream_id), clock=clock)
            )

    if state.book_symbols:
        book_symbols = tuple(state.book_symbols)
        books = await run_on_worker(
            worker,
            CommandPriority.MARKET_POLL,
            lambda c: state.book_poller.poll_once(c, book_symbols),
        )
        for symbol, levels in books:
            stream_id = f"book:{symbol}"
            await websocket.send_json(
                _book_envelope(symbol, levels, seq=state.next_local_seq(stream_id), clock=clock)
            )


async def _publish_executions(
    websocket: WebSocket, state: _ConnectionState, *, sequence_log: SequenceLog
) -> None:
    if not state.executions_subscribed:
        return
    result = sequence_log.read_from_no_transaction(
        stream="executions", from_seq=state.executions_next_seq
    )
    if result.status is ResumeStatus.RESYNC_REQUIRED:
        await websocket.send_json({"channel": "executions", "type": "RESYNC_REQUIRED"})
        state.executions_subscribed = False
        return
    for event in result.events:
        await websocket.send_json(_execution_envelope(event))
        state.executions_next_seq = event.seq + 1


async def _publish_status(
    websocket: WebSocket,
    state: _ConnectionState,
    *,
    kill_switch: KillSwitch,
    terminal_watchdog_state: TerminalWatchdogState,
    worker: BridgeWorker,
    trading_day_policy: TradingDayPolicy,
    auction_window: timedelta,
    clock: Clock,
) -> None:
    if not state.status_subscribed:
        return
    kill_switch_state = kill_switch.status().state.value
    terminal_connected = terminal_watchdog_state.connected
    current = (kill_switch_state, terminal_connected)
    if current != state.last_status_sent:
        await websocket.send_json(
            _status_envelope(
                kill_switch_state=kill_switch_state,
                terminal_connected=terminal_connected,
                symbol=None,
                trading_phase=None,
                seq=state.next_local_seq("status"),
                clock=clock,
            )
        )
        state.last_status_sent = current

    # Trading-phase evidence (audit F4b, plan §114 "status ... publishes
    # ... trading-phase changes when detectable") for whatever symbols this
    # connection already watches through the market-data channels -- never a
    # broker-confirmed exchange phase, only this bridge's own session-window
    # approximation (core.trading_phase).
    symbols = state.tick_symbols | {symbol for symbol, _ in state.bar_symbols} | state.book_symbols
    if not symbols:
        return
    local_now = trading_day_policy.localize(clock.now_utc())

    def read_symbols(client: Mt5Client) -> dict[str, str]:
        phases: dict[str, str] = {}
        for symbol in symbols:
            info = client.symbol_info(symbol)
            if info is not None:
                phases[symbol] = classify_trading_phase(
                    info, now=local_now, auction_window=auction_window
                ).value
        return phases

    phases = await run_on_worker(worker, CommandPriority.MARKET_POLL, read_symbols)
    for symbol in sorted(phases):
        phase = phases[symbol]
        if state.last_phase_sent.get(symbol) == phase:
            continue
        await websocket.send_json(
            _status_envelope(
                kill_switch_state=kill_switch_state,
                terminal_connected=terminal_connected,
                symbol=symbol,
                trading_phase=phase,
                seq=state.next_local_seq("status"),
                clock=clock,
            )
        )
        state.last_phase_sent[symbol] = phase


async def _publish_account(
    websocket: WebSocket, state: _ConnectionState, *, worker: BridgeWorker, clock: Clock
) -> None:
    if not state.account_subscribed:
        return
    account = await run_on_worker(worker, CommandPriority.MARKET_POLL, lambda c: c.account_info())
    snapshot = (
        str(account.balance),
        str(account.equity),
        str(account.margin),
        str(account.margin_free),
    )
    if snapshot == state.last_account_sent:
        return
    balance, equity, margin, margin_free = snapshot
    await websocket.send_json(
        _account_envelope(
            balance=balance,
            equity=equity,
            margin=margin,
            margin_free=margin_free,
            seq=state.next_local_seq("account"),
            clock=clock,
        )
    )
    state.last_account_sent = snapshot


async def run_stream_connection(
    websocket: WebSocket,
    *,
    worker: BridgeWorker,
    sequence_log: SequenceLog,
    kill_switch: KillSwitch,
    terminal_watchdog_state: TerminalWatchdogState,
    trading_day_policy: TradingDayPolicy,
    auction_window: timedelta,
    clock: Clock,
    poll_interval: float = DEFAULT_POLL_INTERVAL_SECONDS,
) -> None:
    await websocket.accept()
    state = _ConnectionState()
    try:
        while True:
            try:
                raw = await asyncio.wait_for(websocket.receive_json(), timeout=poll_interval)
                with stream_operation_span(raw):
                    await _handle_client_message(websocket, raw, state)
            except TimeoutError:
                pass
            except _INVALID_CLIENT_MESSAGE:
                # The client's fault, so not the 1011 an escaping exception
                # would produce (RFC 6455 §7.4.1: 1011 is a server failure).
                await websocket.close(
                    code=INVALID_MESSAGE_CLOSE_CODE, reason="invalid client message"
                )
                return
            await _publish_market_data(websocket, state, worker=worker, clock=clock)
            await _publish_executions(websocket, state, sequence_log=sequence_log)
            await _publish_status(
                websocket,
                state,
                kill_switch=kill_switch,
                terminal_watchdog_state=terminal_watchdog_state,
                worker=worker,
                trading_day_policy=trading_day_policy,
                auction_window=auction_window,
                clock=clock,
            )
            await _publish_account(websocket, state, worker=worker, clock=clock)
    except WebSocketDisconnect:
        return
