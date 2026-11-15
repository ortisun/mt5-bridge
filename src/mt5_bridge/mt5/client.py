"""``Mt5Client``: the sole contract the bridge uses to speak to a terminal.

Every other module depends on this ``Protocol``, never on a concrete
implementation. ``fake_client.FakeMt5Client`` satisfies it deterministically
for every test; ``real_client.RealMt5Client`` (Windows-only) satisfies it by
calling the ``MetaTrader5`` package. Shaped around named intentions
(``order_send``, ``cancel_order``, ``modify_order``) rather than the real
library's single ``order_send(request_dict)`` entry point, so callers never
touch an untyped request dict; ``real_client.py`` is the one place that
builds MT5's native request shape from these typed intents.
"""

from typing import Protocol

from mt5_bridge.mt5.models import (
    AccountInfo,
    Bar,
    BookLevel,
    ModifyOrderRequest,
    Mt5Credentials,
    Mt5Deal,
    Mt5ErrorInfo,
    Mt5Order,
    Mt5Position,
    NewOrderIntent,
    OrderCheckResult,
    OrderSendResult,
    SymbolInfo,
    TerminalAttachment,
    TerminalInfo,
    Tick,
    Timeframe,
)


class Mt5Client(Protocol):
    """Everything the bridge needs from a terminal. Nothing more.

    Methods mirror the real MetaTrader5 Python API's shape closely enough
    that ``real_client.py`` is a mechanical, low-risk translation, while
    staying strictly typed end to end (no raw dict request/response).
    """

    def connect(self, credentials: Mt5Credentials) -> None:
        """Establish the terminal session (``initialize`` + ``login``)."""
        ...

    def attach(self, attachment: TerminalAttachment) -> None:
        """Attach to the already logged-in terminal (``initialize`` without a login)."""
        ...

    def disconnect(self) -> None:
        """Tear down the terminal session (``shutdown``)."""
        ...

    def terminal_info(self) -> TerminalInfo: ...

    def account_info(self) -> AccountInfo: ...

    def symbols(self, group: str | None = None) -> tuple[SymbolInfo, ...]: ...

    def symbol_info(self, symbol: str) -> SymbolInfo | None: ...

    def symbol_tick(self, symbol: str) -> Tick | None: ...

    def copy_bars(
        self, symbol: str, timeframe: Timeframe, start_pos: int, count: int
    ) -> tuple[Bar, ...]:
        """Return up to ``count`` bars starting at ``start_pos`` bars back
        from the current one. ``start_pos=0`` is the still-forming bar;
        callers that only want closed bars pass ``start_pos>=1`` (plan
        rule 28) — and must still check ``Bar.is_closed`` themselves."""
        ...

    def copy_ticks(self, symbol: str, date_from_msc: int, count: int) -> tuple[Tick, ...]: ...

    def order_book(self, symbol: str) -> tuple[BookLevel, ...] | None: ...

    def orders(self) -> tuple[Mt5Order, ...]: ...

    def positions(self) -> tuple[Mt5Position, ...]: ...

    def history_orders(self, date_from_epoch: int, date_to_epoch: int) -> tuple[Mt5Order, ...]: ...

    def history_deals(self, date_from_epoch: int, date_to_epoch: int) -> tuple[Mt5Deal, ...]: ...

    def order_check(self, intent: NewOrderIntent) -> OrderCheckResult: ...

    def order_send(self, intent: NewOrderIntent) -> OrderSendResult: ...

    def modify_order(self, request: ModifyOrderRequest) -> OrderSendResult: ...

    def cancel_order(self, ticket: int) -> OrderSendResult: ...

    def last_error(self) -> Mt5ErrorInfo: ...
