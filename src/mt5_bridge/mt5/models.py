"""Value types exchanged with the MT5 terminal contract.

These types describe what ``Mt5Client`` (client.py) returns and accepts. They
carry raw, source-declared time fields (never a pre-converted ``datetime``):
canonicalizing a raw epoch into a UTC-aware instant is ``core``'s
responsibility (``core/time_normalizer.py``), which reads the unit declared
here rather than inferring it (plan rule 30, §114 "Time").

``Tick.source_time_msc`` and ``Bar.source_time`` are set only by a
wall-clock-converting ``Mt5Client`` decorator (``mt5/terminal_time.py``'s
``ServerWallClockTerminal``) to the epoch the terminal actually reported,
before that decorator overwrites ``time_msc``/``time`` with the converted
true-UTC value. An undecorated client (``fake_client.FakeMt5Client``,
``real_client.RealMt5Client`` on a terminal proven not to need the
conversion) leaves them ``None``, so an envelope builder can tell "no
conversion happened here" from "the raw value equals the converted one."

Prices, volumes and money amounts are ``Decimal`` (plan rule 33); the single
tested boundary that turns a broker's native ``float`` into ``Decimal`` is
``real_client.py`` (Windows-only, unvalidated on this host). ``fake_client.py``
never goes through that boundary because it never holds a native float.
"""

from dataclasses import dataclass, field
from datetime import time
from decimal import Decimal
from enum import Enum, IntEnum, StrEnum, auto


class SourceTimeUnit(StrEnum):
    """Unit a raw terminal timestamp is declared in (plan rule 30).

    The MetaTrader5 Python API documents tick times in milliseconds
    (``time_msc``) and bar/order/deal times in seconds (§116,
    https://www.mql5.com/en/docs/python_metatrader5/mt5copyratesfrom_py).
    """

    SECONDS = "seconds"
    MILLISECONDS = "milliseconds"


class TimeBasis(StrEnum):
    """What a message envelope's ``ts_utc`` represents."""

    EXCHANGE_REPORTED = "exchange_reported"
    """Parsed from a raw time field the terminal attributes to the source."""

    BRIDGE_OBSERVED = "bridge_observed"
    """No source timestamp exists; ``ts_utc`` is when the bridge observed it."""


class OrderSide(StrEnum):
    BUY = "BUY"
    SELL = "SELL"


class OrderType(StrEnum):
    MARKET = "MARKET"
    LIMIT = "LIMIT"
    STOP = "STOP"
    STOP_LIMIT = "STOP_LIMIT"


class TimeInForce(StrEnum):
    DAY = "DAY"
    GTC = "GTC"
    IOC = "IOC"
    FOK = "FOK"
    GTD = "GTD"


class FillingMode(StrEnum):
    """Mirrors MQL5 ``ORDER_FILLING_*``. Read per symbol; never hardcoded."""

    FOK = "FOK"
    IOC = "IOC"
    RETURN = "RETURN"


class PositionAccounting(StrEnum):
    NETTING = "NETTING"
    HEDGING = "HEDGING"


class AccountTradeMode(StrEnum):
    """Mirrors MQL5 ``ACCOUNT_TRADE_MODE_*``: what kind of account the terminal is on."""

    DEMO = "DEMO"
    CONTEST = "CONTEST"
    REAL = "REAL"


class Mt5OrderState(Enum):
    """Mirrors MQL5 ``ORDER_STATE_*`` (see mql5.com/en/docs/constants).

    Members are intentionally unordered (``auto()``): only ``real_client.py``
    maps the terminal's real integer constants onto these names, and that
    mapping is unvalidated on this host. Bridge logic never depends on the
    underlying value, only on the member identity.
    """

    STARTED = auto()
    PLACED = auto()
    PARTIAL = auto()
    FILLED = auto()
    CANCELED = auto()
    REJECTED = auto()
    EXPIRED = auto()
    REQUEST_ADD = auto()
    REQUEST_MODIFY = auto()
    REQUEST_CANCEL = auto()


class DealEntry(StrEnum):
    IN = "IN"
    OUT = "OUT"
    INOUT = "INOUT"
    OUT_BY = "OUT_BY"


class BookSide(StrEnum):
    BUY = "BUY"
    SELL = "SELL"


class Mt5RetCode(IntEnum):
    """Subset of MQL5 ``TRADE_RETCODE_*`` used by this bridge.

    Values mirror the documented MetaTrader5 trade server return codes
    (mql5.com/en/docs/constants/errorswarnings/enum_trade_return_codes).
    Only the codes this bridge branches on are declared; an unrecognized
    real terminal code must still surface as its raw integer (never
    silently coerced), which is why callers keep the raw int alongside
    this enum wherever a real terminal is involved.
    """

    REQUOTE = 10004
    REJECT = 10006
    CANCEL = 10007
    PLACED = 10008
    DONE = 10009
    DONE_PARTIAL = 10010
    ERROR = 10011
    TIMEOUT = 10012
    INVALID = 10013
    INVALID_VOLUME = 10014
    INVALID_PRICE = 10015
    INVALID_STOPS = 10016
    TRADE_DISABLED = 10017
    MARKET_CLOSED = 10018
    NO_MONEY = 10019
    PRICE_CHANGED = 10020
    PRICE_OFF = 10021
    INVALID_EXPIRATION = 10022
    ORDER_CHANGED = 10023
    TOO_MANY_REQUESTS = 10024
    NO_CHANGES = 10025
    INVALID_FILL = 10030
    CONNECTION = 10031


class Timeframe(StrEnum):
    M1 = "M1"
    M5 = "M5"
    M15 = "M15"
    M30 = "M30"
    H1 = "H1"
    H4 = "H4"
    D1 = "D1"
    W1 = "W1"
    MN1 = "MN1"


@dataclass(frozen=True, slots=True)
class SessionWindow:
    """One trading session window declared by the broker for a symbol."""

    weekday: int
    """ISO weekday: 0 = Monday .. 6 = Sunday."""

    open_time: time
    close_time: time


@dataclass(frozen=True, slots=True)
class SymbolInfo:
    """Per-symbol trading rules, always read fresh — never hardcoded (§114)."""

    name: str
    description: str
    currency_base: str
    currency_profit: str
    currency_margin: str
    digits: int
    tick_size: Decimal
    tick_value: Decimal
    volume_min: Decimal
    volume_max: Decimal
    volume_step: Decimal
    contract_size: Decimal
    filling_modes: frozenset[FillingMode]
    time_in_force_modes: frozenset[TimeInForce]
    is_continuous_series: bool
    """True for a broker's synthetic continuous-futures series (e.g. a
    perpetual "WIN$" roll product). The bridge trades only dated contracts;
    order validation rejects submissions against a continuous series (§114
    "B3 Specifics")."""
    trading_allowed: bool
    sessions: tuple[SessionWindow, ...]


class TradingPhase(StrEnum):
    """Trading-phase evidence the bridge can actually derive from a symbol's
    own ``trading_allowed``/``sessions`` data (plan §114 "B3 Specifics":
    "terminal visibility of trading phases is limited"). This is a bridge-side
    approximation from session windows, never a broker-confirmed exchange
    phase; real MT5 phase qualification against B3's own auction schedule is
    external to this classification (ADR-078)."""

    CONTINUOUS = "CONTINUOUS"
    PRE_OPEN_AUCTION = "PRE_OPEN_AUCTION"
    CLOSING_AUCTION = "CLOSING_AUCTION"
    HALTED = "HALTED"
    CLOSED = "CLOSED"


@dataclass(frozen=True, slots=True)
class Tick:
    symbol: str
    time_msc: int
    """Milliseconds since the Unix epoch, as documented for the MT5 tick
    ``time_msc`` field (§116). True UTC once any wall-clock decorator has
    run; see ``source_time_msc`` for what the terminal reported before that."""
    bid: Decimal
    ask: Decimal
    last: Decimal
    volume: Decimal
    source_time_msc: int | None = None
    """The terminal's own, unconverted epoch — set only by
    ``ServerWallClockTerminal`` (``mt5/terminal_time.py``), never inferred.
    ``None`` when no wall-clock conversion happened (module docstring)."""


@dataclass(frozen=True, slots=True)
class Bar:
    symbol: str
    timeframe: Timeframe
    time: int
    """Bar open time, seconds since the Unix epoch (MT5 ``copy_rates_*``
    ``time`` field, §116). True UTC once any wall-clock decorator has run;
    see ``source_time`` for what the terminal reported before that."""
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: Decimal
    is_closed: bool
    """``copy_rates_from_pos`` position 0 is the still-forming bar. This
    bridge never trusts the raw client to have excluded it: callers must
    check this flag (plan rule 28)."""
    source_time: int | None = None
    """The terminal's own, unconverted epoch — set only by
    ``ServerWallClockTerminal`` (``mt5/terminal_time.py``), never inferred.
    ``None`` when no wall-clock conversion happened (module docstring)."""


@dataclass(frozen=True, slots=True)
class BookLevel:
    side: BookSide
    price: Decimal
    volume: Decimal


@dataclass(frozen=True, slots=True)
class AccountInfo:
    login: int
    balance: Decimal
    equity: Decimal
    margin: Decimal
    margin_free: Decimal
    currency: str
    position_accounting: PositionAccounting
    trade_allowed: bool
    server: str
    trade_mode: AccountTradeMode


@dataclass(frozen=True, slots=True)
class TerminalInfo:
    connected: bool
    algo_trading_enabled: bool
    build: int
    name: str
    company: str


@dataclass(frozen=True, slots=True)
class Mt5Credentials:
    login: int
    password: str = field(repr=False)
    server: str
    path: str | None = None


@dataclass(frozen=True, slots=True)
class TerminalAttachment:
    """Attach to a terminal already logged in inside the interactive session.

    No credential crosses the bridge: the broker login stays in the terminal
    (plan §114, "broker credentials remain only in the VM"); the boot check
    then verifies which account the terminal is actually on.
    """

    path: str | None = None


@dataclass(frozen=True, slots=True)
class Mt5ErrorInfo:
    code: int
    description: str


@dataclass(frozen=True, slots=True)
class NewOrderIntent:
    """What the bridge asks the terminal to do. Never persisted verbatim as
    the idempotency payload (see ``core.order_journal``), but hashed from it.
    """

    symbol: str
    side: OrderSide
    order_type: OrderType
    volume: Decimal
    time_in_force: TimeInForce
    filling_mode: FillingMode
    magic: int
    comment: str
    price: Decimal | None = None
    stop_loss: Decimal | None = None
    take_profit: Decimal | None = None
    expiration: int | None = None


@dataclass(frozen=True, slots=True)
class ModifyOrderRequest:
    ticket: int
    price: Decimal | None = None
    stop_loss: Decimal | None = None
    take_profit: Decimal | None = None
    expiration: int | None = None


@dataclass(frozen=True, slots=True)
class OrderCheckResult:
    retcode: Mt5RetCode
    comment: str
    balance: Decimal
    equity: Decimal
    margin: Decimal
    margin_free: Decimal


@dataclass(frozen=True, slots=True)
class OrderSendResult:
    retcode: Mt5RetCode
    order: int | None
    deal: int | None
    volume: Decimal
    price: Decimal
    comment: str


@dataclass(frozen=True, slots=True)
class Mt5Order:
    ticket: int
    symbol: str
    side: OrderSide
    order_type: OrderType
    state: Mt5OrderState
    volume_initial: Decimal
    volume_current: Decimal
    price_open: Decimal
    stop_loss: Decimal | None
    take_profit: Decimal | None
    time_setup: int
    time_expiration: int | None
    magic: int
    comment: str
    time_in_force: TimeInForce
    filling_mode: FillingMode


@dataclass(frozen=True, slots=True)
class Mt5Position:
    ticket: int
    symbol: str
    side: OrderSide
    volume: Decimal
    price_open: Decimal
    price_current: Decimal
    swap: Decimal
    profit: Decimal
    time_open: int
    magic: int
    comment: str


@dataclass(frozen=True, slots=True)
class Mt5Deal:
    ticket: int
    order_ticket: int
    symbol: str
    side: OrderSide
    entry: DealEntry
    volume: Decimal
    price: Decimal
    commission: Decimal
    swap: Decimal
    profit: Decimal
    time_msc: int
    magic: int
    comment: str


__all__ = [
    "AccountInfo",
    "Bar",
    "BookLevel",
    "BookSide",
    "DealEntry",
    "FillingMode",
    "Mt5Credentials",
    "Mt5Deal",
    "Mt5ErrorInfo",
    "Mt5Order",
    "Mt5OrderState",
    "Mt5Position",
    "Mt5RetCode",
    "ModifyOrderRequest",
    "NewOrderIntent",
    "OrderCheckResult",
    "OrderSendResult",
    "OrderSide",
    "OrderType",
    "PositionAccounting",
    "SessionWindow",
    "SourceTimeUnit",
    "SymbolInfo",
    "Tick",
    "Timeframe",
    "TimeBasis",
    "TimeInForce",
    "TerminalInfo",
]
