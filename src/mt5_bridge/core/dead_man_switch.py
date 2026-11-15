"""Lease-based dead-man switch (plan §107, §114).

Every heartbeat (re)establishes the lease. The switch does not act on expiry
itself; the caller (the worker loop) checks ``is_expired`` and drives
``core.emergency_actions.cancel_all_open_orders``.
"""


class DeadManSwitch:
    def __init__(self, *, timeout_seconds: float) -> None:
        self._timeout_ms = round(timeout_seconds * 1000)
        self._expires_at_ms: int | None = None

    @property
    def armed(self) -> bool:
        """Whether a lease is established."""
        return self._expires_at_ms is not None

    def heartbeat(self, *, now_ms: int) -> None:
        """(Re)establish the lease: it now expires one timeout after ``now_ms``."""
        self._expires_at_ms = now_ms + self._timeout_ms

    def is_expired(self, now_ms: int) -> bool:
        """True only once a lease existed and its deadline has passed.

        A switch that never received a heartbeat is not "expired": there is
        no baseline silence to measure against yet.
        """
        return self._expires_at_ms is not None and self._expires_at_ms <= now_ms

    def disarm(self) -> None:
        """Consume an expiry that has been acted on.

        The switch fires once per lease: after CANCEL_ALL it stays silent until
        the next heartbeat, so a rearm of the kill switch holds and the
        terminal is not re-cancelled on every sweep.
        """
        self._expires_at_ms = None
