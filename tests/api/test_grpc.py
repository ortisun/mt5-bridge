"""The gRPC API: the same results, the same problems and the same audit trail as REST."""

from decimal import Decimal
from typing import cast

import grpc
import pytest

from mt5_bridge.api.grpc_api.calls import peer_address
from mt5_bridge.api.grpc_api.generated import mt5_bridge_pb2 as pb
from mt5_bridge.api.grpc_api.generated import mt5_bridge_pb2_grpc as pb_grpc
from mt5_bridge.api.grpc_api.telemetry import Context
from mt5_bridge.core import ocsf
from mt5_bridge.core.kill_switch import KillSwitchState
from mt5_bridge.mt5.fake_client import FakeMt5Client
from mt5_bridge.mt5.models import BookLevel, BookSide

from testkit.app_builder import build_test_app, build_test_app_state
from testkit.contracts import assert_is_ocsf_event
from testkit.fixtures import WINZ25
from testkit.grpc_client import problem_of, serve_grpc
from testkit.http_client import TypedTestClient

pytestmark = pytest.mark.asyncio


def order_request(client_order_id: str = "co-1", *, volume: str = "1") -> pb.OrderRequest:
    return pb.OrderRequest(
        client_order_id=client_order_id,
        symbol=WINZ25,
        side=pb.ORDER_SIDE_BUY,
        type=pb.ORDER_TYPE_MARKET,
        volume=volume,
        time_in_force=pb.TIME_IN_FORCE_DAY,
    )


def quote(fake: FakeMt5Client) -> None:
    """A market order needs a fresh quote to bound its notional."""
    fake.set_tick(WINZ25, bid=Decimal(100), ask=Decimal(101), last=Decimal(100), volume=Decimal(1))


class TestProblems:
    """A failed call ends with the status of its problem type and carries the
    members of the REST problem document."""

    async def test_a_failed_call_carries_the_members_of_the_problem(self) -> None:
        state, _fake = build_test_app_state()
        async with serve_grpc(state) as channel:
            with pytest.raises(grpc.aio.AioRpcError) as failed:
                await pb_grpc.TradingStub(channel).GetOrder(
                    pb.GetOrderRequest(client_order_id="co-1")
                )

        problem = problem_of(failed.value)
        assert failed.value.code() is grpc.StatusCode.NOT_FOUND
        assert failed.value.details() == problem.detail
        assert problem.type == "urn:mt5-bridge:problem:order-not-found"
        assert problem.code == "OrderNotFound"
        assert problem.instance == "/mt5_bridge.v1.Trading/GetOrder"
        assert problem.retryable is False

    async def test_an_invalid_value_names_the_request_field(self) -> None:
        state, fake = build_test_app_state()
        quote(fake)
        async with serve_grpc(state) as channel:
            with pytest.raises(grpc.aio.AioRpcError) as failed:
                await pb_grpc.TradingStub(channel).SubmitOrder(order_request(volume="one"))

        problem = problem_of(failed.value)
        assert failed.value.code() is grpc.StatusCode.INVALID_ARGUMENT
        assert problem.code == "ValidationError"
        assert [(error.field, error.detail) for error in problem.errors] == [
            ("volume", "'one' is not a valid decimal string")
        ]

    async def test_a_disconnected_terminal_is_unavailable_and_says_when_to_retry(self) -> None:
        state, fake = build_test_app_state()
        fake.simulate_disconnect()
        async with serve_grpc(state) as channel:
            with pytest.raises(grpc.aio.AioRpcError) as failed:
                await pb_grpc.MarketDataStub(channel).GetAccount(pb.GetAccountRequest())

        problem = problem_of(failed.value)
        assert failed.value.code() is grpc.StatusCode.UNAVAILABLE
        assert problem.code == "TerminalUnavailable"
        assert (problem.retryable, problem.retry_after_seconds) == (True, 5)

    async def test_amending_an_order_that_is_not_resting_is_a_failed_precondition(self) -> None:
        state, fake = build_test_app_state()
        quote(fake)
        async with serve_grpc(state) as channel:
            stub = pb_grpc.TradingStub(channel)
            await stub.SubmitOrder(order_request())
            with pytest.raises(grpc.aio.AioRpcError) as failed:
                await stub.AmendOrder(pb.AmendOrderRequest(client_order_id="co-1", price="101"))

        assert failed.value.code() is grpc.StatusCode.FAILED_PRECONDITION
        assert problem_of(failed.value).code == "OrderStateConflict"

    async def test_an_unhandled_exception_is_internal_and_quotes_nothing(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        state, _fake = build_test_app_state()

        def fail(self: FakeMt5Client) -> None:
            raise RuntimeError("password=hunter2 at /srv/bridge/secret.py")

        monkeypatch.setattr(FakeMt5Client, "terminal_info", fail)
        async with serve_grpc(state) as channel:
            with pytest.raises(grpc.aio.AioRpcError) as failed:
                await pb_grpc.MarketDataStub(channel).GetTerminal(pb.GetTerminalRequest())

        problem = problem_of(failed.value)
        assert failed.value.code() is grpc.StatusCode.INTERNAL
        assert problem.code == "InternalError"
        assert "hunter2" not in problem.detail
        assert "hunter2" not in (failed.value.details() or "")
        # The cause is in the bridge log, as the detail says.
        (logged,) = [record for record in caplog.records if record.name == "mt5_bridge.grpc"]
        assert logged.exc_info is not None
        assert "GetTerminal" in logged.getMessage()


class TestMarketData:
    """Each answer is compared with what REST returns for the same state."""

    async def test_health_reports_what_rest_reports(self) -> None:
        app, _fake, state = build_test_app()
        with TypedTestClient(app) as http:
            rest = http.get("/v1/health").json()
        async with serve_grpc(state) as channel:
            health = await pb_grpc.MarketDataStub(channel).GetHealth(pb.GetHealthRequest())

        assert health.status == rest["status"] == "ok"
        assert health.terminal_connected is rest["terminal_connected"]
        assert pb.KillSwitchState.Name(health.kill_switch_state) == (
            "KILL_SWITCH_STATE_" + rest["kill_switch_state"]
        )
        # No poll has run: the latency is absent, not zero.
        assert not health.HasField("execution_poll_latency_seconds")
        assert rest["execution_poll_latency_seconds"] is None

    async def test_account_money_is_the_same_decimal_strings_as_rest(self) -> None:
        app, _fake, state = build_test_app()
        with TypedTestClient(app) as http:
            rest = http.get("/v1/account").json()
        async with serve_grpc(state) as channel:
            account = await pb_grpc.MarketDataStub(channel).GetAccount(pb.GetAccountRequest())

        assert account.login == rest["login"]
        assert (account.balance, account.equity) == (rest["balance"], rest["equity"])
        assert (account.margin, account.margin_free) == (rest["margin"], rest["margin_free"])
        assert account.position_accounting == pb.POSITION_ACCOUNTING_NETTING

    async def test_symbols_and_symbol_detail_match_rest(self) -> None:
        app, _fake, state = build_test_app()
        with TypedTestClient(app) as http:
            rest_list = http.get("/v1/symbols").json()
            rest = http.get(f"/v1/symbols/{WINZ25}").json()
        async with serve_grpc(state) as channel:
            stub = pb_grpc.MarketDataStub(channel)
            listed = await stub.ListSymbols(pb.ListSymbolsRequest())
            detail = await stub.GetSymbol(pb.GetSymbolRequest(symbol=WINZ25))

        assert [s.name for s in listed.symbols] == [s["name"] for s in rest_list]
        assert detail.tick_size == rest["tick_size"]
        assert (detail.volume_min, detail.volume_step) == (rest["volume_min"], rest["volume_step"])
        assert [pb.FillingMode.Name(m) for m in detail.filling_modes] == [
            "FILLING_MODE_" + m for m in rest["filling_modes"]
        ]
        assert pb.TradingPhase.Name(detail.trading_phase) == (
            "TRADING_PHASE_" + rest["trading_phase"]
        )

    async def test_an_unknown_symbol_is_not_found(self) -> None:
        state, _fake = build_test_app_state()
        async with serve_grpc(state) as channel:
            with pytest.raises(grpc.aio.AioRpcError) as failed:
                await pb_grpc.MarketDataStub(channel).GetSymbol(pb.GetSymbolRequest(symbol="NOPE"))

        assert failed.value.code() is grpc.StatusCode.NOT_FOUND
        assert problem_of(failed.value).code == "NotFound"

    async def test_bars_without_a_timeframe_are_an_invalid_argument(self) -> None:
        state, _fake = build_test_app_state()
        async with serve_grpc(state) as channel:
            with pytest.raises(grpc.aio.AioRpcError) as failed:
                await pb_grpc.MarketDataStub(channel).ListBars(pb.ListBarsRequest(symbol=WINZ25))

        assert failed.value.code() is grpc.StatusCode.INVALID_ARGUMENT
        assert problem_of(failed.value).code == "ValidationError"

    async def test_the_latest_tick_matches_rest_and_is_absent_without_a_quote(self) -> None:
        app, fake, state = build_test_app()
        async with serve_grpc(state) as channel:
            stub = pb_grpc.MarketDataStub(channel)
            request = pb.GetLatestTickRequest(symbol=WINZ25)
            before = await stub.GetLatestTick(request)
            fake.set_tick(
                WINZ25,
                bid=Decimal("100.5"),
                ask=Decimal("101"),
                last=Decimal("100.5"),
                volume=Decimal(1),
            )
            after = await stub.GetLatestTick(request)
        with TypedTestClient(app) as http:
            rest = http.get("/v1/ticks/latest", params={"symbol": WINZ25}).json()

        assert not before.HasField("tick")
        assert (after.tick.bid, after.tick.ask) == (rest[0]["bid"], rest[0]["ask"])
        assert after.tick.bid == "100.5"

    async def test_the_book_matches_rest(self) -> None:
        app, fake, state = build_test_app()
        fake.set_book(
            WINZ25,
            (
                BookLevel(side=BookSide.SELL, price=Decimal(101), volume=Decimal(5)),
                BookLevel(side=BookSide.BUY, price=Decimal(100), volume=Decimal(10)),
            ),
        )
        with TypedTestClient(app) as http:
            rest = http.get(f"/v1/book/{WINZ25}").json()
        async with serve_grpc(state) as channel:
            book = await pb_grpc.MarketDataStub(channel).GetBook(pb.GetBookRequest(symbol=WINZ25))

        assert [(pb.BookSide.Name(x.side), x.price, x.volume) for x in book.levels] == [
            ("BOOK_SIDE_" + x["side"], x["price"], x["volume"]) for x in rest
        ]
        assert len(book.levels) == 2


class TestTrading:
    async def test_a_submitted_order_is_the_one_rest_then_reads(self) -> None:
        app, fake, state = build_test_app()
        quote(fake)
        async with serve_grpc(state) as channel:
            response = await pb_grpc.TradingStub(channel).SubmitOrder(order_request())
        with TypedTestClient(app) as http:
            rest = http.get("/v1/orders/co-1").json()

        assert response.replayed is False
        assert response.order.client_order_id == "co-1"
        assert pb.OrderState.Name(response.order.state) == "ORDER_STATE_" + rest["state"]
        assert response.order.state == pb.ORDER_STATE_FILLED
        assert response.order.HasField("ticket")
        assert response.order.ticket == rest["ticket"]
        assert response.order.volume == rest["volume"] == "1"
        assert response.order.side == pb.ORDER_SIDE_BUY

    async def test_repeating_a_submit_replays_the_stored_order(self) -> None:
        state, fake = build_test_app_state()
        quote(fake)
        async with serve_grpc(state) as channel:
            stub = pb_grpc.TradingStub(channel)
            first = await stub.SubmitOrder(order_request())
            second = await stub.SubmitOrder(order_request())

        assert (first.replayed, second.replayed) == (False, True)
        assert second.order == first.order

    async def test_reusing_a_client_order_id_with_another_payload_already_exists(self) -> None:
        state, fake = build_test_app_state()
        quote(fake)
        async with serve_grpc(state) as channel:
            stub = pb_grpc.TradingStub(channel)
            await stub.SubmitOrder(order_request())
            with pytest.raises(grpc.aio.AioRpcError) as failed:
                await stub.SubmitOrder(order_request(volume="2"))

        assert failed.value.code() is grpc.StatusCode.ALREADY_EXISTS
        assert problem_of(failed.value).code == "IdempotencyConflict"

    async def test_an_order_submitted_through_rest_replays_through_grpc(self) -> None:
        # One journal behind both transports: the idempotency key is shared.
        app, fake, state = build_test_app()
        quote(fake)
        body = {
            "client_order_id": "co-1",
            "symbol": WINZ25,
            "side": "BUY",
            "type": "MARKET",
            "volume": "1",
            "time_in_force": "DAY",
        }
        with TypedTestClient(app) as http:
            created = http.post("/v1/orders", json=body)
        async with serve_grpc(state) as channel:
            response = await pb_grpc.TradingStub(channel).SubmitOrder(order_request())

        assert created.status_code == 201
        assert response.replayed is True
        assert response.order.ticket == created.json()["ticket"]

    @pytest.mark.parametrize("volume", ["", "one", "1,5"])
    async def test_a_volume_that_is_not_a_decimal_is_an_invalid_argument(self, volume: str) -> None:
        state, fake = build_test_app_state()
        quote(fake)
        async with serve_grpc(state) as channel:
            with pytest.raises(grpc.aio.AioRpcError) as failed:
                await pb_grpc.TradingStub(channel).SubmitOrder(order_request(volume=volume))

        assert failed.value.code() is grpc.StatusCode.INVALID_ARGUMENT
        assert state.journal.get_no_transaction("co-1") is None

    async def test_an_order_without_a_side_is_an_invalid_argument(self) -> None:
        state, fake = build_test_app_state()
        quote(fake)
        request = order_request()
        request.ClearField("side")
        async with serve_grpc(state) as channel:
            with pytest.raises(grpc.aio.AioRpcError) as failed:
                await pb_grpc.TradingStub(channel).SubmitOrder(request)

        assert failed.value.code() is grpc.StatusCode.INVALID_ARGUMENT
        assert state.journal.get_no_transaction("co-1") is None

    async def test_an_unknown_order_is_not_found(self) -> None:
        state, fake = build_test_app_state()
        quote(fake)
        async with serve_grpc(state) as channel:
            stub = pb_grpc.TradingStub(channel)
            with pytest.raises(grpc.aio.AioRpcError) as read:
                await stub.GetOrder(pb.GetOrderRequest(client_order_id="nope"))
            with pytest.raises(grpc.aio.AioRpcError) as canceled:
                await stub.CancelOrder(pb.CancelOrderRequest(client_order_id="nope"))

        assert read.value.code() is grpc.StatusCode.NOT_FOUND
        assert canceled.value.code() is grpc.StatusCode.NOT_FOUND
        assert problem_of(canceled.value).code == "OrderNotFound"

    async def test_a_resting_order_is_listed_amended_and_canceled(self) -> None:
        state, fake = build_test_app_state()
        quote(fake)
        limit = order_request()
        limit.type = pb.ORDER_TYPE_LIMIT
        limit.price = "100"
        async with serve_grpc(state) as channel:
            stub = pb_grpc.TradingStub(channel)
            submitted = await stub.SubmitOrder(limit)
            listed = await stub.ListOpenOrders(pb.ListOpenOrdersRequest())
            await stub.AmendOrder(
                pb.AmendOrderRequest(client_order_id="co-1", price="101"),
            )
            (amended,) = fake.orders()
            canceled = await stub.CancelOrder(pb.CancelOrderRequest(client_order_id="co-1"))

        assert submitted.order.price == "100"
        assert [o.client_order_id for o in listed.orders] == ["co-1"]
        assert amended.price_open == Decimal(101)
        assert canceled.state == pb.ORDER_STATE_CANCELED

    async def test_check_order_reports_the_terminal_retcode(self) -> None:
        app, fake, state = build_test_app()
        quote(fake)
        body = {
            "client_order_id": "co-1",
            "symbol": WINZ25,
            "side": "BUY",
            "type": "MARKET",
            "volume": "1",
            "time_in_force": "DAY",
        }
        with TypedTestClient(app) as http:
            rest = http.post("/v1/orders/check", json=body).json()
        async with serve_grpc(state) as channel:
            check = await pb_grpc.TradingStub(channel).CheckOrder(order_request())

        assert check.retcode == rest["retcode"]
        assert (check.margin, check.margin_free) == (rest["margin"], rest["margin_free"])
        assert state.journal.get_no_transaction("co-1") is None

    async def test_the_kill_switch_is_activated_then_rearmed(self) -> None:
        state, fake = build_test_app_state()
        quote(fake)
        async with serve_grpc(state) as channel:
            stub = pb_grpc.TradingStub(channel)
            halted = await stub.ActivateKillSwitch(
                pb.ActivateKillSwitchRequest(action=pb.KILL_SWITCH_ACTION_HALT_NEW_ORDERS),
            )
            state_while_halted = state.kill_switch.status().state
            rearmed = await stub.RearmKillSwitch(pb.RearmKillSwitchRequest())

        assert halted.state == pb.KILL_SWITCH_STATE_HALTED
        assert state_while_halted is KillSwitchState.HALTED
        assert halted.updated_at.ToDatetime() == state.clock.now_utc().replace(tzinfo=None)
        assert rearmed.state == pb.KILL_SWITCH_STATE_NORMAL
        assert state.kill_switch.status().state is KillSwitchState.NORMAL

    async def test_a_kill_switch_request_without_an_action_changes_nothing(self) -> None:
        state, fake = build_test_app_state()
        quote(fake)
        async with serve_grpc(state) as channel:
            with pytest.raises(grpc.aio.AioRpcError) as failed:
                await pb_grpc.TradingStub(channel).ActivateKillSwitch(
                    pb.ActivateKillSwitchRequest()
                )

        assert failed.value.code() is grpc.StatusCode.INVALID_ARGUMENT
        assert state.kill_switch.status().state is KillSwitchState.NORMAL

    async def test_every_heartbeat_holds_the_dead_man_lease(self) -> None:
        state, _fake = build_test_app_state(dead_man_timeout_seconds=30)
        later_ms = state.clock.now_ms() + 31_000
        before = state.dead_man_switch.is_expired(later_ms)
        async with serve_grpc(state) as channel:
            await pb_grpc.TradingStub(channel).Heartbeat(pb.HeartbeatRequest())

        # Without a heartbeat there is no lease to expire; with one there is.
        assert (before, state.dead_man_switch.is_expired(later_ms)) == (False, True)


class TestAudit:
    """A write call is audited like a REST write: an OCSF API Activity event."""

    async def test_a_write_is_recorded_with_the_method_and_the_callers_address(self) -> None:
        state, fake = build_test_app_state()
        quote(fake)
        async with serve_grpc(state) as channel:
            response = await pb_grpc.TradingStub(channel).SubmitOrder(order_request())

        (record,) = state.audit.list_no_transaction()
        event = record.to_ocsf()
        assert_is_ocsf_event(event)
        assert (record.method, record.path) == ("GRPC", "/mt5_bridge.v1.Trading/SubmitOrder")
        assert record.account_id == state.expected_account_id
        assert record.payload_hash is not None
        assert record.src_host == "127.0.0.1"
        assert record.src_port is not None
        assert "ORDER_STATE_" + record.outcome == pb.OrderState.Name(response.order.state)
        assert event["class_uid"] == ocsf.API_ACTIVITY.uid
        assert event["activity_id"] == ocsf.ApiActivity.CREATE
        assert event["api"] == {"operation": "mt5_bridge.v1.Trading/SubmitOrder"}
        assert event["actor"] == {"user": {"uid": state.expected_account_id}}
        assert event["src_endpoint"] == {"ip": "127.0.0.1", "port": record.src_port}
        assert "http_request" not in event

    async def test_each_write_has_the_activity_of_what_it_does(self) -> None:
        state, fake = build_test_app_state()
        quote(fake)
        limit = order_request()
        limit.type = pb.ORDER_TYPE_LIMIT
        limit.price = "100"
        async with serve_grpc(state) as channel:
            stub = pb_grpc.TradingStub(channel)
            await stub.SubmitOrder(limit)
            await stub.AmendOrder(pb.AmendOrderRequest(client_order_id="co-1", price="101"))
            await stub.CancelOrder(pb.CancelOrderRequest(client_order_id="co-1"))
            await stub.ActivateKillSwitch(
                pb.ActivateKillSwitchRequest(action=pb.KILL_SWITCH_ACTION_HALT_NEW_ORDERS)
            )
            await stub.RearmKillSwitch(pb.RearmKillSwitchRequest())
            await stub.GetOrder(pb.GetOrderRequest(client_order_id="co-1"))

        records = state.audit.list_no_transaction()
        assert [(r.path.rpartition("/")[2], r.to_ocsf()["activity_id"]) for r in records] == [
            ("SubmitOrder", ocsf.ApiActivity.CREATE),
            ("AmendOrder", ocsf.ApiActivity.UPDATE),
            ("CancelOrder", ocsf.ApiActivity.DELETE),
            ("ActivateKillSwitch", ocsf.ApiActivity.CREATE),
            ("RearmKillSwitch", ocsf.ApiActivity.DELETE),
        ]
        assert records[-1].payload_hash is None
        assert records[-1].outcome == "NORMAL"

    async def test_a_refused_write_is_not_recorded(self) -> None:
        state, _fake = build_test_app_state()
        async with serve_grpc(state) as channel:
            with pytest.raises(grpc.aio.AioRpcError):
                await pb_grpc.TradingStub(channel).SubmitOrder(order_request(volume="one"))

        assert state.audit.list_no_transaction() == ()


class TestPeerAddress:
    @pytest.mark.parametrize(
        ("peer", "expected"),
        [
            ("ipv4:10.0.0.7:5000", ("10.0.0.7", 5000)),
            ("ipv6:[::1]:5000", ("::1", 5000)),
            ("ipv6:%5B::1%5D:5000", ("::1", 5000)),
            ("unix:/tmp/bridge.sock", None),
            ("", None),
        ],
    )
    async def test_the_peer_string_gives_the_callers_host_and_port(
        self, peer: str, expected: tuple[str, int] | None
    ) -> None:
        class Peer:
            def peer(self) -> str:
                return peer

        assert peer_address(cast("Context", Peer())) == expected
