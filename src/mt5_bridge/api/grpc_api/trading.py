"""The Trading service: the gRPC form of ``api.routes_orders``."""

from google.protobuf.message import Message

from mt5_bridge.api.errors import (
    ORDER_NOT_FOUND,
    SYMBOL_NOT_FOUND,
    UNSUPPORTED_VENUE_CAPABILITY,
    ProblemError,
)
from mt5_bridge.api.grpc_api import convert
from mt5_bridge.api.grpc_api.calls import Service, record_audit, rpc
from mt5_bridge.api.grpc_api.generated import mt5_bridge_pb2 as pb
from mt5_bridge.api.grpc_api.generated import mt5_bridge_pb2_grpc as pb_grpc
from mt5_bridge.api.grpc_api.telemetry import Context
from mt5_bridge.api.run_on_worker import run_on_worker
from mt5_bridge.core.kill_switch import KillSwitchAction
from mt5_bridge.core.order_service import ReplaceOrderSpec, select_filling_mode
from mt5_bridge.core.worker import CommandPriority
from mt5_bridge.mt5.models import NewOrderIntent


class TradingService(Service, pb_grpc.TradingServicer):
    service_name = pb.DESCRIPTOR.services_by_name["Trading"].full_name

    def _audit(
        self, context: Context, rpc_name: str, request: Message | None, outcome: str
    ) -> None:
        record_audit(
            self.state,
            context,
            method=f"{self.service_name}/{rpc_name}",
            request=request,
            outcome=outcome,
        )

    @rpc
    async def ListOpenOrders(
        self, request: pb.ListOpenOrdersRequest, context: Context
    ) -> pb.ListOrdersResponse:
        state = self.state
        return pb.ListOrdersResponse(
            orders=[convert.order(e) for e in state.journal.list_open_no_transaction()]
        )

    @rpc
    async def GetOrder(self, request: pb.GetOrderRequest, context: Context) -> pb.Order:
        state = self.state
        entry = state.journal.get_no_transaction(request.client_order_id)
        if entry is None:
            raise ProblemError(
                ORDER_NOT_FOUND, f"unknown client_order_id {request.client_order_id!r}"
            )
        return convert.order(entry)

    @rpc
    async def ListPositions(
        self, request: pb.ListPositionsRequest, context: Context
    ) -> pb.ListPositionsResponse:
        state = self.state
        positions = await run_on_worker(
            state.worker, CommandPriority.MARKET_POLL, lambda c: c.positions()
        )
        return pb.ListPositionsResponse(
            positions=[
                pb.Position(
                    symbol=p.symbol,
                    side=convert.ORDER_SIDE.to_pb(p.side),
                    volume=str(p.volume),
                    price_open=str(p.price_open),
                    price_current=str(p.price_current),
                )
                for p in positions
            ]
        )

    @rpc
    async def ListHistoryOrders(
        self, request: pb.HistoryRequest, context: Context
    ) -> pb.ListHistoryOrdersResponse:
        state = self.state
        from_epoch, to_epoch = request.from_epoch, request.to_epoch
        orders = await run_on_worker(
            state.worker,
            CommandPriority.EXECUTION_POLL,
            lambda c: c.history_orders(from_epoch, to_epoch),
        )
        return pb.ListHistoryOrdersResponse(
            orders=[
                pb.HistoryOrder(
                    ticket=o.ticket,
                    symbol=o.symbol,
                    side=convert.ORDER_SIDE.to_pb(o.side),
                    order_type=convert.ORDER_TYPE.to_pb(o.order_type),
                    volume_initial=str(o.volume_initial),
                    volume_current=str(o.volume_current),
                    price_open=str(o.price_open),
                    time_setup=o.time_setup,
                    time_in_force=convert.TIME_IN_FORCE.to_pb(o.time_in_force),
                )
                for o in orders
            ]
        )

    @rpc
    async def ListHistoryDeals(
        self, request: pb.HistoryRequest, context: Context
    ) -> pb.ListHistoryDealsResponse:
        state = self.state
        from_epoch, to_epoch = request.from_epoch, request.to_epoch
        deals = await run_on_worker(
            state.worker,
            CommandPriority.EXECUTION_POLL,
            lambda c: c.history_deals(from_epoch, to_epoch),
        )
        return pb.ListHistoryDealsResponse(
            deals=[
                pb.Deal(
                    ticket=d.ticket,
                    order_ticket=d.order_ticket,
                    symbol=d.symbol,
                    side=convert.ORDER_SIDE.to_pb(d.side),
                    volume=str(d.volume),
                    price=str(d.price),
                    commission=str(d.commission),
                    swap=str(d.swap),
                    profit=str(d.profit),
                    time_msc=d.time_msc,
                )
                for d in deals
            ]
        )

    @rpc
    async def Heartbeat(
        self, request: pb.HeartbeatRequest, context: Context
    ) -> pb.HeartbeatResponse:
        """Every heartbeat (re)establishes the dead-man lease (plan §107)."""
        state = self.state
        state.dead_man_switch.heartbeat(now_ms=state.clock.now_ms())
        return pb.HeartbeatResponse()

    @rpc
    async def CheckOrder(self, request: pb.OrderRequest, context: Context) -> pb.OrderCheck:
        state = self.state
        symbol = request.symbol
        side = convert.ORDER_SIDE.from_pb(request.side, field="side")
        order_type = convert.ORDER_TYPE.from_pb(request.type, field="type")
        time_in_force = convert.TIME_IN_FORCE.from_pb(request.time_in_force, field="time_in_force")
        volume = convert.decimal_from(request.volume, field="volume")
        symbol_info = await run_on_worker(
            state.worker, CommandPriority.ORDER, lambda c: c.symbol_info(symbol)
        )
        if symbol_info is None:
            raise ProblemError(SYMBOL_NOT_FOUND, f"unknown symbol {symbol!r}")
        filling_mode = select_filling_mode(symbol_info.filling_modes)
        if filling_mode is None:
            raise ProblemError(
                UNSUPPORTED_VENUE_CAPABILITY, "symbol advertises no supported filling mode"
            )
        intent = NewOrderIntent(
            symbol=symbol,
            side=side,
            order_type=order_type,
            volume=volume,
            price=convert.optional_decimal(request, "price"),
            stop_loss=convert.optional_decimal(request, "stop_loss"),
            take_profit=convert.optional_decimal(request, "take_profit"),
            time_in_force=time_in_force,
            filling_mode=filling_mode,
            expiration=convert.optional_int(request, "expiration"),
            magic=state.settings.magic,
            comment="check",
        )
        result = await run_on_worker(
            state.worker, CommandPriority.ORDER, lambda c: c.order_check(intent)
        )
        return pb.OrderCheck(
            retcode=int(result.retcode),
            comment=result.comment,
            balance=str(result.balance),
            equity=str(result.equity),
            margin=str(result.margin),
            margin_free=str(result.margin_free),
        )

    @rpc
    async def SubmitOrder(
        self, request: pb.OrderRequest, context: Context
    ) -> pb.SubmitOrderResponse:
        state = self.state
        result = await state.order_service.submit_order(
            client_order_id=request.client_order_id,
            symbol=request.symbol,
            side=convert.ORDER_SIDE.from_pb(request.side, field="side"),
            order_type=convert.ORDER_TYPE.from_pb(request.type, field="type"),
            volume=convert.decimal_from(request.volume, field="volume"),
            time_in_force=convert.TIME_IN_FORCE.from_pb(
                request.time_in_force, field="time_in_force"
            ),
            price=convert.optional_decimal(request, "price"),
            stop_loss=convert.optional_decimal(request, "stop_loss"),
            take_profit=convert.optional_decimal(request, "take_profit"),
            expiration=convert.optional_int(request, "expiration"),
        )
        self._audit(context, "SubmitOrder", request, result.entry.state.value)
        return pb.SubmitOrderResponse(order=convert.order(result.entry), replayed=result.replayed)

    @rpc
    async def AmendOrder(self, request: pb.AmendOrderRequest, context: Context) -> pb.Order:
        state = self.state
        entry = await state.order_service.amend_order(
            request.client_order_id,
            price=convert.optional_decimal(request, "price"),
            stop_loss=convert.optional_decimal(request, "stop_loss"),
            take_profit=convert.optional_decimal(request, "take_profit"),
            expiration=convert.optional_int(request, "expiration"),
        )
        self._audit(context, "AmendOrder", request, entry.state.value)
        return convert.order(entry)

    @rpc
    async def ReplaceOrder(
        self, request: pb.ReplaceOrderRequest, context: Context
    ) -> pb.ReplaceOrderResponse:
        state = self.state
        result = await state.order_service.replace_order(
            operation_id=request.operation_id,
            original_client_order_id=request.client_order_id,
            new_client_order_id=request.new_client_order_id,
            new_order=ReplaceOrderSpec(
                symbol=request.symbol,
                side=convert.ORDER_SIDE.from_pb(request.side, field="side"),
                order_type=convert.ORDER_TYPE.from_pb(request.type, field="type"),
                volume=convert.decimal_from(request.volume, field="volume"),
                time_in_force=convert.TIME_IN_FORCE.from_pb(
                    request.time_in_force, field="time_in_force"
                ),
                price=convert.optional_decimal(request, "price"),
                stop_loss=convert.optional_decimal(request, "stop_loss"),
                take_profit=convert.optional_decimal(request, "take_profit"),
                expiration=convert.optional_int(request, "expiration"),
            ),
        )
        self._audit(context, "ReplaceOrder", request, result.outcome.value)
        return pb.ReplaceOrderResponse(
            operation_id=request.operation_id,
            outcome=convert.REPLACE_OUTCOME.to_pb(result.outcome),
            original=None if result.original is None else convert.order(result.original),
            new=None if result.new is None else convert.order(result.new),
        )

    @rpc
    async def CancelOrder(self, request: pb.CancelOrderRequest, context: Context) -> pb.Order:
        state = self.state
        entry = await state.order_service.cancel_order(request.client_order_id)
        self._audit(context, "CancelOrder", request, entry.state.value)
        return convert.order(entry)

    @rpc
    async def ActivateKillSwitch(
        self, request: pb.ActivateKillSwitchRequest, context: Context
    ) -> pb.KillSwitchStatus:
        state = self.state
        action = convert.KILL_SWITCH_ACTION.from_pb(request.action, field="action")
        status = state.kill_switch.activate(
            action, reason="requested through the API", now=state.clock.now_utc()
        )
        # Activating the switch only flips its persisted state; the reduction
        # (plan §107 EmergencyReductionPolicy) is a deliberate, separate step so
        # the two effects are independently observable in the audit trail.
        if action is KillSwitchAction.CANCEL_ALL:
            await state.order_service.cancel_all()
        elif action is KillSwitchAction.FLATTEN:
            await state.order_service.flatten()
        self._audit(context, "ActivateKillSwitch", request, status.state.value)
        return convert.kill_switch_status(status)

    @rpc
    async def RearmKillSwitch(
        self, request: pb.RearmKillSwitchRequest, context: Context
    ) -> pb.KillSwitchStatus:
        state = self.state
        status = state.kill_switch.rearm(now=state.clock.now_utc())
        self._audit(context, "RearmKillSwitch", None, status.state.value)
        return convert.kill_switch_status(status)

    @rpc
    async def ReconcileOrder(self, request: pb.ReconcileOrderRequest, context: Context) -> pb.Order:
        """Re-examine one UNKNOWN order against the terminal (plan §56)."""
        state = self.state
        entry = await state.order_service.reconcile_order(request.client_order_id)
        self._audit(context, "ReconcileOrder", request, entry.state.value)
        return convert.order(entry)
