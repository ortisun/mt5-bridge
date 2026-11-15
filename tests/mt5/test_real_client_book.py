"""``RealMt5Client`` depth against a scripted ``MetaTrader5`` module (075).

The real package ships only Windows wheels, so each test installs a stand-in
module under that name and imports the real client fresh against it: every
constant the client maps at import time gets a distinct integer, and only
the depth calls are scripted. The 075 demo run showed the ``book`` channel
never streamed because depth was never requested from the terminal.
"""

import sys
import types
from collections.abc import Callable, Iterator
from decimal import Decimal
from itertools import count
from typing import Any, NamedTuple

import pytest

from mt5_bridge.mt5.models import BookLevel, BookSide

BOOK_TYPE_SELL = 1
BOOK_TYPE_BUY = 2
BOOK_TYPE_SELL_MARKET = 3
BOOK_TYPE_BUY_MARKET = 4


class BookInfo(NamedTuple):
    type: int
    price: float
    volume: int
    volume_dbl: float


class ScriptedMt5(types.ModuleType):
    def __init__(self) -> None:
        super().__init__("MetaTrader5")
        self._codes = count(1_000)
        self.added: list[str] = []
        self.add_accepts = True
        self.snapshots: dict[str, tuple[BookInfo, ...] | None] = {}
        self.BOOK_TYPE_SELL = BOOK_TYPE_SELL
        self.BOOK_TYPE_BUY = BOOK_TYPE_BUY
        self.BOOK_TYPE_SELL_MARKET = BOOK_TYPE_SELL_MARKET
        self.BOOK_TYPE_BUY_MARKET = BOOK_TYPE_BUY_MARKET

    def __getattr__(self, name: str) -> int:
        if not name.isupper():
            raise AttributeError(name)
        code = next(self._codes)
        setattr(self, name, code)
        return code

    def market_book_add(self, symbol: str) -> bool:
        self.added.append(symbol)
        return self.add_accepts

    def market_book_get(self, symbol: str) -> tuple[BookInfo, ...] | None:
        return self.snapshots.get(symbol)

    def shutdown(self) -> None:
        return None


@pytest.fixture
def terminal(monkeypatch: pytest.MonkeyPatch) -> Iterator[tuple[ScriptedMt5, Callable[[], Any]]]:
    scripted = ScriptedMt5()
    monkeypatch.setitem(sys.modules, "MetaTrader5", scripted)
    sys.modules.pop("mt5_bridge.mt5.real_client", None)

    def make_client() -> Any:
        from mt5_bridge.mt5.real_client import RealMt5Client

        return RealMt5Client()

    yield scripted, make_client
    sys.modules.pop("mt5_bridge.mt5.real_client", None)


def test_depth_subscribes_once_and_maps_both_sides(
    terminal: tuple[ScriptedMt5, Callable[[], Any]],
) -> None:
    scripted, make_client = terminal
    scripted.snapshots["PETR4"] = (
        BookInfo(BOOK_TYPE_SELL, 28.81, 300, 300.0),
        BookInfo(BOOK_TYPE_SELL_MARKET, 28.80, 100, 100.0),
        BookInfo(BOOK_TYPE_BUY, 28.79, 200, 200.0),
        BookInfo(BOOK_TYPE_BUY_MARKET, 28.78, 500, 500.0),
    )
    client = make_client()

    first = client.order_book("PETR4")
    second = client.order_book("PETR4")

    assert scripted.added == ["PETR4"]
    assert (
        first
        == second
        == (
            BookLevel(side=BookSide.SELL, price=Decimal("28.81"), volume=Decimal("300.0")),
            BookLevel(side=BookSide.SELL, price=Decimal("28.8"), volume=Decimal("100.0")),
            BookLevel(side=BookSide.BUY, price=Decimal("28.79"), volume=Decimal("200.0")),
            BookLevel(side=BookSide.BUY, price=Decimal("28.78"), volume=Decimal("500.0")),
        )
    )


def test_a_broker_without_depth_refuses_the_subscription_and_reports_none(
    terminal: tuple[ScriptedMt5, Callable[[], Any]],
) -> None:
    scripted, make_client = terminal
    scripted.add_accepts = False
    client = make_client()

    assert client.order_book("PETR4") is None
    assert client.order_book("PETR4") is None
    assert scripted.added == ["PETR4", "PETR4"]  # retried: depth may start later


def test_a_subscription_without_a_first_snapshot_yet_reports_none(
    terminal: tuple[ScriptedMt5, Callable[[], Any]],
) -> None:
    scripted, make_client = terminal
    client = make_client()

    assert client.order_book("PETR4") is None
    scripted.snapshots["PETR4"] = ()
    assert client.order_book("PETR4") is None
    assert scripted.added == ["PETR4"]


def test_a_reconnect_subscribes_again(
    terminal: tuple[ScriptedMt5, Callable[[], Any]],
) -> None:
    scripted, make_client = terminal
    scripted.snapshots["PETR4"] = (BookInfo(BOOK_TYPE_BUY, 28.79, 200, 200.0),)
    client = make_client()
    client.order_book("PETR4")

    client.disconnect()
    client.order_book("PETR4")

    assert scripted.added == ["PETR4", "PETR4"]
