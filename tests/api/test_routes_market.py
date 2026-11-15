"""GET /v1/ticks: the default start bound is a recent window, not epoch 0."""

from decimal import Decimal

from mt5_bridge.mt5.fake_client import FakeMt5Client

from testkit.app_builder import build_test_app
from testkit.clock import FixedClock
from testkit.fixtures import WINZ25
from testkit.http_client import TypedTestClient

NOW_MS = 1_700_000_000_000
DEFAULT_LOOKBACK_SECONDS = 300


def push_tick(client: FakeMt5Client, *, price: Decimal) -> None:
    client.set_tick(WINZ25, bid=price, ask=price, last=price, volume=Decimal(1))


class TestTicksDefaultWindow:
    def test_omitting_from_time_msc_excludes_a_tick_older_than_the_default_window(self) -> None:
        clock = FixedClock(NOW_MS)
        app, fake_client, _state = build_test_app(clock=clock)
        push_tick(fake_client, price=Decimal(100))
        clock.advance(DEFAULT_LOOKBACK_SECONDS + 1)
        push_tick(fake_client, price=Decimal(200))

        with TypedTestClient(app) as http:
            response = http.get("/v1/ticks", params={"symbol": WINZ25})

        body = response.json()
        assert len(body) == 1
        assert body[0]["bid"] == "200"

    def test_an_explicit_from_time_msc_of_zero_still_returns_the_oldest_tick(self) -> None:
        clock = FixedClock(NOW_MS)
        app, fake_client, _state = build_test_app(clock=clock)
        push_tick(fake_client, price=Decimal(100))
        clock.advance(DEFAULT_LOOKBACK_SECONDS + 1)
        push_tick(fake_client, price=Decimal(200))

        with TypedTestClient(app) as http:
            response = http.get(
                "/v1/ticks",
                params={"symbol": WINZ25, "from_time_msc": 0},
            )

        body = response.json()
        assert [row["bid"] for row in body] == ["100", "200"]

    def test_count_still_limits_results_within_the_default_window(self) -> None:
        clock = FixedClock(NOW_MS)
        app, fake_client, _state = build_test_app(clock=clock)
        push_tick(fake_client, price=Decimal(100))
        clock.advance(1)
        push_tick(fake_client, price=Decimal(200))

        with TypedTestClient(app) as http:
            response = http.get(
                "/v1/ticks",
                params={"symbol": WINZ25, "count": 1},
            )

        body = response.json()
        assert len(body) == 1
        assert body[0]["bid"] == "100"


def test_latest_tick_returns_the_current_quote_not_the_oldest() -> None:
    clock = FixedClock(1_700_000_000_000)
    app, fake_client, _state = build_test_app(clock=clock)
    push_tick(fake_client, price=Decimal(10))
    clock.advance(1)
    push_tick(fake_client, price=Decimal(11))
    with TypedTestClient(app) as http:
        latest = http.get(f"/v1/ticks/latest?symbol={WINZ25}")

    assert latest.status_code == 200
    assert [row["last"] for row in latest.json()] == ["11"]


def test_latest_tick_of_a_symbol_without_quotes_is_empty() -> None:
    app, _fake_client, _state = build_test_app()
    with TypedTestClient(app) as http:
        latest = http.get("/v1/ticks/latest?symbol=NOQUOTE")

    assert latest.status_code == 200
    assert latest.json() == []


_SESSION_OPEN_MS = 1_700_060_400_000
"""2023-11-15 15:00:00 UTC = 12:00 America/Sao_Paulo (Wednesday), inside
WINZ25's declared 10:00-17:00 session (testkit.fixtures._b3_equity_sessions)."""


class TestSymbolTradingPhase:
    def test_within_the_session_reports_continuous(self) -> None:
        clock = FixedClock(_SESSION_OPEN_MS)
        app, _fake_client, _state = build_test_app(clock=clock)
        with TypedTestClient(app) as http:
            response = http.get(f"/v1/symbols/{WINZ25}")

        assert response.json()["trading_phase"] == "CONTINUOUS"

    def test_well_outside_the_session_reports_closed(self) -> None:
        clock = FixedClock(NOW_MS)  # 2023-11-14 22:13:20 UTC = 19:13 America/Sao_Paulo
        app, _fake_client, _state = build_test_app(clock=clock)
        with TypedTestClient(app) as http:
            response = http.get(f"/v1/symbols/{WINZ25}")

        assert response.json()["trading_phase"] == "CLOSED"


class TestHealthAuctionCapability:
    def test_defaults_to_unsupported(self) -> None:
        app, _fake_client, _state = build_test_app()
        with TypedTestClient(app) as http:
            response = http.get("/v1/health")

        assert response.json()["auction_participation_supported"] is False

    def test_reflects_an_explicitly_supported_deployment(self) -> None:
        app, _fake_client, _state = build_test_app(auction_participation_supported=True)
        with TypedTestClient(app) as http:
            response = http.get("/v1/health")

        assert response.json()["auction_participation_supported"] is True
