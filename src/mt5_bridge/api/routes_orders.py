"""Orders, positions, history, order lifecycle, kill switch, heartbeat."""

import hashlib

from fastapi import APIRouter, Request, Response
from pydantic import BaseModel

from mt5_bridge.api.app_state import get_state
from mt5_bridge.api.errors import (
    ORDER_NOT_FOUND,
    SYMBOL_NOT_FOUND,
    UNSUPPORTED_VENUE_CAPABILITY,
    VALIDATION_ERROR,
    ProblemError,
)
from mt5_bridge.api.run_on_worker import run_on_worker
from mt5_bridge.api.schemas import (
    AmendOrderRequest,
    DealResponse,
    HistoryOrderResponse,
    KillSwitchRequest,
    KillSwitchResponse,
    OrderCheckResponse,
    OrderResponse,
    PositionResponse,
    ReplaceOrderRequest,
    ReplaceOrderResponse,
    SubmitOrderRequest,
)
from mt5_bridge.core.kill_switch import KillSwitchAction
from mt5_bridge.core.order_journal import JournalEntry
from mt5_bridge.core.order_service import ReplaceOrderSpec, select_filling_mode
from mt5_bridge.core.worker import CommandPriority
from mt5_bridge.mt5.models import NewOrderIntent

router = APIRouter(prefix="/v1", tags=["orders"])


def _entry_to_response(entry: JournalEntry) -> OrderResponse:
    return OrderResponse(
        client_order_id=entry.client_order_id,
        symbol=entry.symbol,
        side=entry.side,
        order_type=entry.order_type,
        volume=entry.volume,
        price=entry.price,
        time_in_force=entry.time_in_force,
        state=entry.state,
        ticket=entry.ticket,
        mt5_retcode=entry.mt5_retcode,
        mt5_last_error=entry.mt5_last_error,
        reason=entry.reason,
    )


def _payload_hash(body: BaseModel) -> str:
    return hashlib.sha256(body.model_dump_json().encode()).hexdigest()


@router.get("/orders", response_model=list[OrderResponse])
async def list_open_orders(*, request: Request) -> list[OrderResponse]:
    state = get_state(request)
    return [_entry_to_response(e) for e in state.journal.list_open_no_transaction()]


@router.get("/orders/{client_order_id}", response_model=OrderResponse)
async def get_order(
    client_order_id: str,
    *,
    request: Request,
) -> OrderResponse:
    state = get_state(request)
    entry = state.journal.get_no_transaction(client_order_id)
    if entry is None:
        raise ProblemError(ORDER_NOT_FOUND, f"unknown client_order_id {client_order_id!r}")
    return _entry_to_response(entry)


@router.get("/positions", response_model=list[PositionResponse])
async def positions(*, request: Request) -> list[PositionResponse]:
    state = get_state(request)
    raw = await run_on_worker(state.worker, CommandPriority.MARKET_POLL, lambda c: c.positions())
    return [
        PositionResponse(
            symbol=p.symbol,
            side=p.side,
            volume=p.volume,
            price_open=p.price_open,
            price_current=p.price_current,
        )
        for p in raw
    ]


@router.get("/history/orders", response_model=list[HistoryOrderResponse])
async def history_orders(
    *,
    request: Request,
    from_epoch: int,
    to_epoch: int,
) -> list[HistoryOrderResponse]:
    state = get_state(request)
    raw = await run_on_worker(
        state.worker,
        CommandPriority.EXECUTION_POLL,
        lambda c: c.history_orders(from_epoch, to_epoch),
    )
    return [
        HistoryOrderResponse(
            ticket=o.ticket,
            symbol=o.symbol,
            side=o.side,
            order_type=o.order_type,
            volume_initial=o.volume_initial,
            volume_current=o.volume_current,
            price_open=o.price_open,
            time_setup=o.time_setup,
            time_in_force=o.time_in_force,
        )
        for o in raw
    ]


@router.get("/history/deals", response_model=list[DealResponse])
async def history_deals(
    *,
    request: Request,
    from_epoch: int,
    to_epoch: int,
) -> list[DealResponse]:
    state = get_state(request)
    raw = await run_on_worker(
        state.worker,
        CommandPriority.EXECUTION_POLL,
        lambda c: c.history_deals(from_epoch, to_epoch),
    )
    return [
        DealResponse(
            ticket=d.ticket,
            order_ticket=d.order_ticket,
            symbol=d.symbol,
            side=d.side,
            volume=d.volume,
            price=d.price,
            commission=d.commission,
            swap=d.swap,
            profit=d.profit,
            time_msc=d.time_msc,
        )
        for d in raw
    ]


@router.post("/orders/check", response_model=OrderCheckResponse)
async def check_order(
    body: SubmitOrderRequest,
    *,
    request: Request,
) -> OrderCheckResponse:
    state = get_state(request)
    symbol_info = await run_on_worker(
        state.worker, CommandPriority.ORDER, lambda c: c.symbol_info(body.symbol)
    )
    if symbol_info is None:
        raise ProblemError(SYMBOL_NOT_FOUND, f"unknown symbol {body.symbol!r}")
    filling_mode = select_filling_mode(symbol_info.filling_modes)
    if filling_mode is None:
        raise ProblemError(
            UNSUPPORTED_VENUE_CAPABILITY, "symbol advertises no supported filling mode"
        )
    intent = NewOrderIntent(
        symbol=body.symbol,
        side=body.side,
        order_type=body.type,
        volume=body.volume,
        price=body.price,
        stop_loss=body.stop_loss,
        take_profit=body.take_profit,
        time_in_force=body.time_in_force,
        filling_mode=filling_mode,
        expiration=body.expiration,
        magic=state.settings.magic,
        comment="check",
    )
    result = await run_on_worker(
        state.worker, CommandPriority.ORDER, lambda c: c.order_check(intent)
    )
    return OrderCheckResponse(
        retcode=int(result.retcode),
        comment=result.comment,
        balance=result.balance,
        equity=result.equity,
        margin=result.margin,
        margin_free=result.margin_free,
    )


@router.post("/orders", response_model=OrderResponse)
async def submit_order(
    body: SubmitOrderRequest,
    response: Response,
    *,
    request: Request,
) -> OrderResponse:
    state = get_state(request)
    payload_hash = _payload_hash(body)
    result = await state.order_service.submit_order(
        client_order_id=body.client_order_id,
        symbol=body.symbol,
        side=body.side,
        order_type=body.type,
        volume=body.volume,
        time_in_force=body.time_in_force,
        price=body.price,
        stop_loss=body.stop_loss,
        take_profit=body.take_profit,
        expiration=body.expiration,
    )
    response.status_code = 200 if result.replayed else 201
    state.audit.record(
        now=state.clock.now_utc(),
        account_id=state.expected_account_id,
        method="POST",
        path="/v1/orders",
        payload_hash=payload_hash,
        outcome=result.entry.state.value,
        client=request.client,
    )
    return _entry_to_response(result.entry)


@router.post("/orders/{client_order_id}/reconcile", response_model=OrderResponse)
async def reconcile_order(client_order_id: str, *, request: Request) -> OrderResponse:
    """Re-examine one UNKNOWN order against the terminal (plan §56)."""
    state = get_state(request)
    entry = await state.order_service.reconcile_order(client_order_id)
    state.audit.record(
        now=state.clock.now_utc(),
        account_id=state.expected_account_id,
        method="POST",
        path=f"/v1/orders/{client_order_id}/reconcile",
        payload_hash=None,
        outcome=entry.state.value,
        client=request.client,
    )
    return _entry_to_response(entry)


@router.patch("/orders/{client_order_id}", response_model=OrderResponse)
async def amend_order(
    client_order_id: str,
    body: AmendOrderRequest,
    *,
    request: Request,
) -> OrderResponse:
    state = get_state(request)
    entry = await state.order_service.amend_order(
        client_order_id,
        price=body.price,
        stop_loss=body.stop_loss,
        take_profit=body.take_profit,
        expiration=body.expiration,
    )
    state.audit.record(
        now=state.clock.now_utc(),
        account_id=state.expected_account_id,
        method="PATCH",
        path=f"/v1/orders/{client_order_id}",
        payload_hash=_payload_hash(body),
        outcome=entry.state.value,
        client=request.client,
    )
    return _entry_to_response(entry)


@router.post("/orders/{client_order_id}/replace", response_model=ReplaceOrderResponse)
async def replace_order(
    client_order_id: str,
    body: ReplaceOrderRequest,
    *,
    request: Request,
) -> ReplaceOrderResponse:
    state = get_state(request)
    result = await state.order_service.replace_order(
        operation_id=body.operation_id,
        original_client_order_id=client_order_id,
        new_client_order_id=body.new_client_order_id,
        new_order=ReplaceOrderSpec(
            symbol=body.symbol,
            side=body.side,
            order_type=body.type,
            volume=body.volume,
            time_in_force=body.time_in_force,
            price=body.price,
            stop_loss=body.stop_loss,
            take_profit=body.take_profit,
            expiration=body.expiration,
        ),
    )
    state.audit.record(
        now=state.clock.now_utc(),
        account_id=state.expected_account_id,
        method="POST",
        path=f"/v1/orders/{client_order_id}/replace",
        payload_hash=_payload_hash(body),
        outcome=result.outcome.value,
        client=request.client,
    )
    return ReplaceOrderResponse(
        operation_id=body.operation_id,
        outcome=result.outcome,
        original=_entry_to_response(result.original) if result.original else None,
        new=_entry_to_response(result.new) if result.new else None,
    )


@router.delete("/orders/{client_order_id}", response_model=OrderResponse)
async def cancel_order(
    client_order_id: str,
    *,
    request: Request,
) -> OrderResponse:
    state = get_state(request)
    entry = await state.order_service.cancel_order(client_order_id)
    state.audit.record(
        now=state.clock.now_utc(),
        account_id=state.expected_account_id,
        method="DELETE",
        path=f"/v1/orders/{client_order_id}",
        payload_hash=None,
        outcome=entry.state.value,
        client=request.client,
    )
    return _entry_to_response(entry)


@router.post("/kill-switch", response_model=KillSwitchResponse)
async def activate_kill_switch(
    body: KillSwitchRequest,
    *,
    request: Request,
) -> KillSwitchResponse:
    state = get_state(request)
    try:
        action = KillSwitchAction(body.action)
    except ValueError as error:
        detail = f"unknown kill-switch action {body.action!r}"
        raise ProblemError(
            VALIDATION_ERROR, detail, errors=[{"pointer": "#/action", "detail": detail}]
        ) from error
    status = state.kill_switch.activate(
        action, reason="requested through the API", now=state.clock.now_utc()
    )
    # Activating the switch only flips its persisted state; the actual
    # reduction (plan §107 EmergencyReductionPolicy) is a deliberate,
    # separate step so the two effects are independently observable in the
    # audit trail.
    if action is KillSwitchAction.CANCEL_ALL:
        await state.order_service.cancel_all()
    elif action is KillSwitchAction.FLATTEN:
        await state.order_service.flatten()
    state.audit.record(
        now=state.clock.now_utc(),
        account_id=state.expected_account_id,
        method="POST",
        path="/v1/kill-switch",
        payload_hash=_payload_hash(body),
        outcome=status.state.value,
        client=request.client,
    )
    return KillSwitchResponse(
        state=status.state.value, reason=status.reason, updated_at=status.updated_at.isoformat()
    )


@router.delete("/kill-switch", response_model=KillSwitchResponse)
async def rearm_kill_switch(*, request: Request) -> KillSwitchResponse:
    state = get_state(request)
    status = state.kill_switch.rearm(now=state.clock.now_utc())
    state.audit.record(
        now=state.clock.now_utc(),
        account_id=state.expected_account_id,
        method="DELETE",
        path="/v1/kill-switch",
        payload_hash=None,
        outcome=status.state.value,
        client=request.client,
    )
    return KillSwitchResponse(
        state=status.state.value, reason=status.reason, updated_at=status.updated_at.isoformat()
    )


@router.post("/heartbeat", status_code=204)
async def heartbeat(*, request: Request) -> None:
    """Every heartbeat (re)establishes the dead-man lease (plan §107)."""
    state = get_state(request)
    state.dead_man_switch.heartbeat(now_ms=state.clock.now_ms())
    return None
