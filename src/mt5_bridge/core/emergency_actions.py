"""The shared payload behind kill-switch CANCEL_ALL/FLATTEN and the dead-man switch.

``cancel_all_open_orders`` never raises on a per-order failure: a rejected or
lost cancel is reported, not thrown, so one bad order cannot stop the sweep
of the rest (plan §107: cancellation is not guaranteed during outages, but
attempted and recorded). ``flatten_positions`` only *builds* reduction
intents — it never sends them — capped at exactly the reconciled position,
on the opposite side, so the caller cannot accidentally open or reverse
exposure by construction (plan §107 EmergencyReductionPolicy).
"""

from dataclasses import dataclass

from mt5_bridge.mt5.client import Mt5Client
from mt5_bridge.mt5.errors import Mt5ConnectionLost
from mt5_bridge.mt5.models import Mt5RetCode, NewOrderIntent, OrderSide, OrderType, TimeInForce


@dataclass(frozen=True, slots=True)
class CancelAllReport:
    attempted: tuple[int, ...]
    canceled: tuple[int, ...]
    failed: tuple[int, ...]


def cancel_all_open_orders(client: Mt5Client) -> CancelAllReport:
    open_orders = client.orders()
    canceled: list[int] = []
    failed: list[int] = []
    for order in open_orders:
        try:
            result = client.cancel_order(order.ticket)
        except Mt5ConnectionLost:
            failed.append(order.ticket)
            continue
        if result.retcode is Mt5RetCode.DONE:
            canceled.append(order.ticket)
        else:
            failed.append(order.ticket)
    return CancelAllReport(
        attempted=tuple(order.ticket for order in open_orders),
        canceled=tuple(canceled),
        failed=tuple(failed),
    )


def flatten_positions(client: Mt5Client, *, magic: int, comment: str) -> tuple[NewOrderIntent, ...]:
    """Build one closing MARKET intent per open position.

    Volume is exactly the currently reconciled position — never rounded up,
    never guessed — and the side is always the opposite of the position, so
    sending every returned intent can only reduce exposure, never add to or
    reverse it. Building intents never sends them: the caller submits each
    through the normal, journaled ``order_send`` path for auditability.
    """
    intents: list[NewOrderIntent] = []
    for position in client.positions():
        if position.volume <= 0:
            continue
        symbol = client.symbol_info(position.symbol)
        if symbol is None or not symbol.filling_modes:
            continue
        closing_side = OrderSide.SELL if position.side is OrderSide.BUY else OrderSide.BUY
        time_in_force = (
            TimeInForce.DAY
            if TimeInForce.DAY in symbol.time_in_force_modes
            else next(iter(symbol.time_in_force_modes))
        )
        intents.append(
            NewOrderIntent(
                symbol=position.symbol,
                side=closing_side,
                order_type=OrderType.MARKET,
                volume=position.volume,
                time_in_force=time_in_force,
                filling_mode=next(iter(symbol.filling_modes)),
                magic=magic,
                comment=comment,
            )
        )
    return tuple(intents)
