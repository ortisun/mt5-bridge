"""Trade-server return codes from the real terminal, translated explicitly.

``Mt5RetCode`` declares only the codes this bridge branches on. A code outside
it is surfaced as ``UnrecognizedRetcode`` carrying the raw integer, never
coerced into a member: the order service then rejects (nothing was sent) or
records ``UNKNOWN`` (something may have been sent). The one documented
translation is ``order_check``'s success value: ``MqlTradeCheckResult`` reports
0 when the check passes (observed as ``(0, "Done")`` on the M8/069 demo
terminal, build 6182), which this bridge reads as ``DONE``.
"""

from mt5_bridge.mt5.models import Mt5RetCode

_ORDER_CHECK_OK = 0


class UnrecognizedRetcode(Exception):
    def __init__(self, raw: int, call: str) -> None:
        super().__init__(f"{call} returned unrecognized trade-server retcode {raw}")
        self.raw = raw
        self.call = call


def trade_retcode(raw: int, *, call: str) -> Mt5RetCode:
    try:
        return Mt5RetCode(raw)
    except ValueError:
        raise UnrecognizedRetcode(raw, call) from None


def order_check_retcode(raw: int) -> Mt5RetCode:
    if raw == _ORDER_CHECK_OK:
        return Mt5RetCode.DONE
    return trade_retcode(raw, call="order_check")
