"""Exceptions any ``Mt5Client`` implementation may raise.

Distinct from a normal rejection (an ``OrderSendResult``/``OrderCheckResult``
with a non-success retcode): these mean the bridge does not know what the
terminal did, which is exactly the trigger for ``UNKNOWN`` handling in
``core.order_service`` rather than for reading a retcode (§56, §114).
"""


class Mt5ConnectionLost(Exception):
    """The terminal is unreachable, or became unreachable mid-call.

    Raised by ``fake_client`` when disconnected and by any queued "lost
    response" scenario. ``real_client`` raises it when the underlying
    ``MetaTrader5`` call returns ``None``/fails without a trade-server
    retcode (IPC failure, terminal not running, etc.).
    """
