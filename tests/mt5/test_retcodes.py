"""Trade-server return codes: documented translations only, never coercion."""

import pytest

from mt5_bridge.mt5.models import Mt5RetCode
from mt5_bridge.mt5.retcodes import UnrecognizedRetcode, order_check_retcode, trade_retcode


def test_order_check_reports_success_as_zero() -> None:
    # Observed on the M8/069 demo terminal (build 6182): order_check -> (0, "Done").
    assert order_check_retcode(0) is Mt5RetCode.DONE


def test_known_codes_map_to_their_members() -> None:
    assert trade_retcode(10018, call="order_send") is Mt5RetCode.MARKET_CLOSED
    assert order_check_retcode(10019) is Mt5RetCode.NO_MONEY


def test_an_unrecognized_code_surfaces_its_raw_value() -> None:
    with pytest.raises(UnrecognizedRetcode) as raised:
        trade_retcode(10099, call="order_send")

    assert raised.value.raw == 10099
    assert raised.value.call == "order_send"


def test_zero_is_only_success_for_order_check() -> None:
    with pytest.raises(UnrecognizedRetcode):
        trade_retcode(0, call="order_send")
