"""GET /v1/bars and /v1/ticks, and their RPCs: a date range read in pages
over the fake terminal. Neither takes a quantity."""

from decimal import Decimal
from typing import Any

import grpc
import pytest

from mt5_bridge.api.grpc_api.generated import mt5_bridge_pb2 as pb
from mt5_bridge.api.grpc_api.generated import mt5_bridge_pb2_grpc as pb_grpc
from mt5_bridge.mt5.fake_client import FakeMt5Client
from mt5_bridge.mt5.models import Timeframe

from testkit.app_builder import build_test_app
from testkit.clock import FixedClock
from testkit.contracts import assert_conforms, load_openapi, openapi_schema_validator
from testkit.fixtures import WINZ25
from testkit.grpc_client import problem_of, serve_grpc
from testkit.http_client import TypedTestClient

OPENAPI = load_openapi()
NOW_MS = 1_700_000_000_000
NOW = NOW_MS // 1000
MINUTE = 60
# 2500 one-minute bars: three pages of a thousand bar lengths.
BAR_COUNT = 2500
# 2500 ticks, 10 ms apart: three pages of a thousand ticks.
TICK_COUNT = 2500


def push_minute_bars(fake: FakeMt5Client, count: int = BAR_COUNT) -> list[int]:
    """``count`` closed M1 bars from NOW on; the one after them is still forming."""
    times = [NOW + i * MINUTE for i in range(count + 1)]
    for time in times:
        fake.push_bar(WINZ25, Timeframe.M1, time=time, o=1, h=2, low=1, c=2, volume=10)
    return times[:-1]


def push_ticks(fake: FakeMt5Client, clock: FixedClock, count: int = TICK_COUNT) -> None:
    """``count`` ticks 10 ms apart from NOW_MS on; the bid is the position."""
    for index in range(count):
        clock.set_ms(NOW_MS + index * 10)
        price = Decimal(index)
        fake.set_tick(WINZ25, bid=price, ask=price, last=price, volume=Decimal(1))


def bars_query(**overrides: object) -> dict[str, object]:
    return {
        "symbol": WINZ25,
        "timeframe": "M1",
        "from_epoch": NOW,
        "to_epoch": NOW + BAR_COUNT * MINUTE,
        **overrides,
    }


def ticks_query(**overrides: object) -> dict[str, object]:
    return {
        "symbol": WINZ25,
        "from_time_msc": NOW_MS,
        "to_time_msc": NOW_MS + 60_000,
        **overrides,
    }


def bars_request(**overrides: Any) -> pb.ListBarsRequest:
    fields: dict[str, Any] = {
        "symbol": WINZ25,
        "timeframe": pb.TIMEFRAME_M1,
        "from_epoch": NOW,
        "to_epoch": NOW + BAR_COUNT * MINUTE,
        **overrides,
    }
    return pb.ListBarsRequest(**fields)


def ticks_request(**overrides: Any) -> pb.ListTicksRequest:
    fields: dict[str, Any] = {
        "symbol": WINZ25,
        "from_time_msc": NOW_MS,
        "to_time_msc": NOW_MS + 60_000,
        **overrides,
    }
    return pb.ListTicksRequest(**fields)


class TestBars:
    def test_every_page_reports_the_same_page_count_and_the_next_page(self) -> None:
        app, fake, _state = build_test_app()
        push_minute_bars(fake)

        with TypedTestClient(app) as http:
            pages = [
                http.get("/v1/bars", params=bars_query(page=page)).json() for page in (1, 2, 3)
            ]

        assert [page["total_pages"] for page in pages] == [3, 3, 3]
        assert [page["next_page"] for page in pages] == [2, 3, None]
        assert [len(page["bars"]) for page in pages] == [1000, 1000, 500]

    def test_following_next_page_returns_every_bar_of_the_range_once(self) -> None:
        app, fake, _state = build_test_app()
        times = push_minute_bars(fake)

        read: list[int] = []
        page: int | None = 1
        with TypedTestClient(app) as http:
            while page is not None:
                body = http.get("/v1/bars", params=bars_query(page=page)).json()
                read.extend(bar["time"] for bar in body["bars"])
                page = body["next_page"]

        assert read == times

    def test_the_page_defaults_to_the_first_and_the_answer_matches_the_contract(self) -> None:
        app, fake, _state = build_test_app()
        push_minute_bars(fake, 3)

        with TypedTestClient(app) as http:
            response = http.get("/v1/bars", params=bars_query(to_epoch=NOW + 10 * MINUTE))

        body = response.json()
        assert_conforms(openapi_schema_validator(OPENAPI, "BarsPage"), body)
        assert (body["page"], body["total_pages"], body["next_page"]) == (1, 1, None)
        assert [bar["time"] for bar in body["bars"]] == [NOW, NOW + MINUTE, NOW + 2 * MINUTE]
        assert body["bars"][0]["open"] == "1"

    @pytest.mark.parametrize("missing", ["timeframe", "from_epoch", "to_epoch"])
    def test_the_range_and_the_timeframe_are_required(self, missing: str) -> None:
        app, _fake, _state = build_test_app()
        params = bars_query()
        del params[missing]

        with TypedTestClient(app) as http:
            response = http.get("/v1/bars", params=params)

        assert response.status_code == 422
        assert [(e["in"], e["parameter"]) for e in response.json()["errors"]] == [
            ("query", missing)
        ]

    @pytest.mark.parametrize(
        ("overrides", "parameter"),
        [({"to_epoch": NOW}, "to_epoch"), ({"page": 4}, "page"), ({"page": 0}, "page")],
    )
    def test_a_range_or_page_that_cannot_be_read_is_a_located_validation_problem(
        self, overrides: dict[str, object], parameter: str
    ) -> None:
        app, _fake, _state = build_test_app()

        with TypedTestClient(app) as http:
            response = http.get("/v1/bars", params=bars_query(**overrides))

        body = response.json()
        assert response.status_code == 422
        assert_conforms(openapi_schema_validator(OPENAPI, "Problem"), body)
        assert body["code"] == "ValidationError"
        assert [(e["in"], e["parameter"]) for e in body["errors"]] == [("query", parameter)]

    @pytest.mark.asyncio
    async def test_the_rpc_returns_the_page_rest_returns(self) -> None:
        app, fake, state = build_test_app()
        push_minute_bars(fake)
        with TypedTestClient(app) as http:
            rest = http.get("/v1/bars", params=bars_query(page=2)).json()

        async with serve_grpc(state) as channel:
            stub = pb_grpc.MarketDataStub(channel)
            second = await stub.ListBars(bars_request(page=2))
            last = await stub.ListBars(bars_request(page=3))

        assert (second.page, second.total_pages, second.next_page) == (
            rest["page"],
            rest["total_pages"],
            rest["next_page"],
        )
        assert [(b.time, b.open, b.high, b.low, b.close, b.volume) for b in second.bars] == [
            (b["time"], b["open"], b["high"], b["low"], b["close"], b["volume"])
            for b in rest["bars"]
        ]
        assert all(b.timeframe == pb.TIMEFRAME_M1 for b in second.bars)
        assert not last.HasField("next_page")

    @pytest.mark.asyncio
    async def test_the_rpc_refuses_a_page_outside_the_range(self) -> None:
        _app, _fake, state = build_test_app()

        async with serve_grpc(state) as channel:
            with pytest.raises(grpc.aio.AioRpcError) as failed:
                await pb_grpc.MarketDataStub(channel).ListBars(
                    bars_request(to_epoch=NOW + MINUTE, page=2)
                )

        assert failed.value.code() is grpc.StatusCode.INVALID_ARGUMENT
        assert problem_of(failed.value).code == "ValidationError"


class TestTicks:
    def test_following_next_cursor_returns_every_tick_of_the_range_once(self) -> None:
        clock = FixedClock(NOW_MS)
        app, fake, _state = build_test_app(clock=clock)
        push_ticks(fake, clock)

        pages: list[list[str]] = []
        cursor: str | None = None
        with TypedTestClient(app) as http:
            while True:
                params = ticks_query() if cursor is None else ticks_query(cursor=cursor)
                body: dict[str, Any] = http.get("/v1/ticks", params=params).json()
                assert_conforms(openapi_schema_validator(OPENAPI, "TicksPage"), body)
                pages.append([tick["bid"] for tick in body["ticks"]])
                cursor = body["next_cursor"]
                if cursor is None:
                    break

        assert [len(page) for page in pages] == [1000, 1000, 500]
        assert [bid for page in pages for bid in page] == [str(i) for i in range(TICK_COUNT)]

    def test_ticks_at_or_after_the_end_of_the_range_are_left_out(self) -> None:
        clock = FixedClock(NOW_MS)
        app, fake, _state = build_test_app(clock=clock)
        push_ticks(fake, clock, 11)

        with TypedTestClient(app) as http:
            body = http.get("/v1/ticks", params=ticks_query(to_time_msc=NOW_MS + 20)).json()

        assert [tick["bid"] for tick in body["ticks"]] == ["0", "1"]
        assert body["next_cursor"] is None

    @pytest.mark.parametrize("missing", ["from_time_msc", "to_time_msc"])
    def test_the_range_is_required(self, missing: str) -> None:
        app, _fake, _state = build_test_app()
        params = ticks_query()
        del params[missing]

        with TypedTestClient(app) as http:
            response = http.get("/v1/ticks", params=params)

        assert response.status_code == 422
        assert [(e["in"], e["parameter"]) for e in response.json()["errors"]] == [
            ("query", missing)
        ]

    @pytest.mark.parametrize(
        ("overrides", "parameter"),
        [
            ({"to_time_msc": NOW_MS}, "to_time_msc"),
            ({"cursor": "not-a-cursor"}, "cursor"),
            ({"cursor": f"{NOW_MS + 60_000}.0"}, "cursor"),
        ],
    )
    def test_a_range_or_cursor_that_cannot_be_read_is_a_located_validation_problem(
        self, overrides: dict[str, object], parameter: str
    ) -> None:
        app, _fake, _state = build_test_app()

        with TypedTestClient(app) as http:
            response = http.get("/v1/ticks", params=ticks_query(**overrides))

        body = response.json()
        assert response.status_code == 422
        assert_conforms(openapi_schema_validator(OPENAPI, "Problem"), body)
        assert [(e["in"], e["parameter"]) for e in body["errors"]] == [("query", parameter)]

    @pytest.mark.asyncio
    async def test_the_rpc_pages_with_the_cursor_rest_returns(self) -> None:
        clock = FixedClock(NOW_MS)
        app, fake, state = build_test_app(clock=clock)
        push_ticks(fake, clock, 1002)
        with TypedTestClient(app) as http:
            rest = http.get("/v1/ticks", params=ticks_query()).json()

        async with serve_grpc(state) as channel:
            stub = pb_grpc.MarketDataStub(channel)
            first = await stub.ListTicks(ticks_request())
            second = await stub.ListTicks(ticks_request(cursor=first.next_cursor))

        assert first.next_cursor == rest["next_cursor"]
        assert [tick.bid for tick in first.ticks] == [tick["bid"] for tick in rest["ticks"]]
        assert [tick.bid for tick in second.ticks] == ["1000", "1001"]
        assert not second.HasField("next_cursor")

    @pytest.mark.asyncio
    async def test_the_rpc_refuses_a_cursor_it_did_not_return(self) -> None:
        _app, _fake, state = build_test_app()

        async with serve_grpc(state) as channel:
            with pytest.raises(grpc.aio.AioRpcError) as failed:
                await pb_grpc.MarketDataStub(channel).ListTicks(
                    ticks_request(to_time_msc=NOW_MS + 1000, cursor="x")
                )

        assert failed.value.code() is grpc.StatusCode.INVALID_ARGUMENT
        assert problem_of(failed.value).code == "ValidationError"
