"""Real /v1/stream tick and bar events: ``metadata.original_time`` must show what
the terminal reported, not what a wall-clock decorator computed from it.

Regression coverage for the M8/069 evidence in
``mt5/terminal_time.py``'s module docstring: the Rico-DEMO terminal reports
its server's wall clock as if it were UTC, so ``ServerWallClockTerminal``
converts the session-open epoch of 10:03 to the true UTC instant 13:03. A
``time`` of 13:03Z is correct; a ``metadata.original_time`` that also reads 13:03
would hide the terminal's own report behind the bridge's conversion.
"""

from datetime import UTC, datetime
from decimal import Decimal

from mt5_bridge.mt5.fake_client import FakeMt5Client
from mt5_bridge.mt5.models import Timeframe
from mt5_bridge.mt5.terminal_time import ServerWallClock, ServerWallClockTerminal

from testkit.app_builder import build_test_app
from testkit.clock import FixedClock
from testkit.fixtures import WINZ25
from testkit.http_client import TypedTestClient

SAO_PAULO = ServerWallClock("America/Sao_Paulo")
# 2026-09-25 10:03 on the server's wall clock, reported as an epoch.
WALL_1003 = int(datetime(2026, 9, 25, 10, 3, tzinfo=UTC).timestamp())
UTC_1303 = int(datetime(2026, 9, 25, 13, 3, tzinfo=UTC).timestamp())


def wrap_with_sao_paulo(fake: FakeMt5Client) -> ServerWallClockTerminal:
    return ServerWallClockTerminal(fake, SAO_PAULO)


def test_a_wrapped_terminals_tick_envelope_keeps_the_terminals_raw_epoch() -> None:
    clock = FixedClock(WALL_1003 * 1000)
    app, fake_client, _state = build_test_app(clock=clock, terminal_wrapper=wrap_with_sao_paulo)
    fake_client.set_tick(WINZ25, bid=Decimal(1), ask=Decimal(2), last=Decimal(1), volume=Decimal(1))

    with TypedTestClient(app) as http:
        with http.websocket_connect("/v1/stream") as ws:
            ws.send_json({"op": "subscribe", "channel": "ticks", "symbols": [WINZ25]})
            message = ws.receive_json()

    assert message["time"] == UTC_1303 * 1000
    assert message["metadata"]["original_time"] == str(WALL_1003 * 1000)


def test_an_unwrapped_terminals_tick_envelope_is_unchanged() -> None:
    clock = FixedClock(WALL_1003 * 1000)
    app, fake_client, _state = build_test_app(clock=clock)
    fake_client.set_tick(WINZ25, bid=Decimal(1), ask=Decimal(2), last=Decimal(1), volume=Decimal(1))

    with TypedTestClient(app) as http:
        with http.websocket_connect("/v1/stream") as ws:
            ws.send_json({"op": "subscribe", "channel": "ticks", "symbols": [WINZ25]})
            message = ws.receive_json()

    assert message["time"] == WALL_1003 * 1000
    assert message["metadata"]["original_time"] == str(WALL_1003 * 1000)


def test_a_wrapped_terminals_bar_envelope_keeps_the_terminals_raw_epoch() -> None:
    clock = FixedClock(WALL_1003 * 1000)
    app, fake_client, _state = build_test_app(clock=clock, terminal_wrapper=wrap_with_sao_paulo)
    fake_client.push_bar(WINZ25, Timeframe.M1, time=WALL_1003, o=1, h=1, low=1, c=1, volume=1)

    with TypedTestClient(app) as http:
        with http.websocket_connect("/v1/stream") as ws:
            ws.send_json(
                {"op": "subscribe", "channel": "bars", "symbols": [WINZ25], "timeframe": "M1"}
            )
            fake_client.push_bar(
                WINZ25, Timeframe.M1, time=WALL_1003 + 60, o=1, h=1, low=1, c=1, volume=1
            )
            message = ws.receive_json()

    assert message["time"] == UTC_1303 * 1000
    assert message["metadata"]["original_time"] == str(WALL_1003)
    assert message["unmapped"]["source_time_unit"] == "seconds"


def test_an_unwrapped_terminals_bar_envelope_is_unchanged() -> None:
    clock = FixedClock(WALL_1003 * 1000)
    app, fake_client, _state = build_test_app(clock=clock)
    fake_client.push_bar(WINZ25, Timeframe.M1, time=WALL_1003, o=1, h=1, low=1, c=1, volume=1)

    with TypedTestClient(app) as http:
        with http.websocket_connect("/v1/stream") as ws:
            ws.send_json(
                {"op": "subscribe", "channel": "bars", "symbols": [WINZ25], "timeframe": "M1"}
            )
            fake_client.push_bar(
                WINZ25, Timeframe.M1, time=WALL_1003 + 60, o=1, h=1, low=1, c=1, volume=1
            )
            message = ws.receive_json()

    assert message["time"] == WALL_1003 * 1000
    assert message["metadata"]["original_time"] == str(WALL_1003)
