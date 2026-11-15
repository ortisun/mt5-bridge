"""Clock drift: the gap between this process's clock and a trusted reference.

Plan §114: "the VM clock is synchronized (w32time/NTP); drift above the
threshold appears in /v1/health and blocks orders." Synchronizing the clock
is the VM's job (w32time); this module supplies the *check* side — a
``ClockDriftProbe`` measures the gap, ``within_threshold`` is the pure
comparison ``OrderService`` and the health route both use, and
``measure_drift_into`` is the periodic-loop wiring: it keeps the last known
good value on a failed probe rather than reporting a false zero.
``ntp_drift_probe`` (a real UDP SNTP query) is production-only — no test in
this project performs real network I/O; tests inject a fake probe.
"""

import socket
import struct
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Protocol

from mt5_bridge.clock import Clock

_NTP_EPOCH_OFFSET_SECONDS = 2_208_988_800
"""Seconds between the NTP epoch (1900-01-01) and the Unix epoch (1970-01-01)."""


class ClockDriftProbe(Protocol):
    def __call__(self) -> float:
        """Signed seconds: reference time minus this process's clock."""
        ...


@dataclass
class ClockDriftState:
    """Shared, mutable last-known drift — read by ``OrderService`` (to reject
    orders) and the health route (to report it), written by the periodic
    measurement loop."""

    drift_seconds: float = 0.0


def within_threshold(drift_seconds: float, threshold_seconds: float) -> bool:
    return abs(drift_seconds) <= threshold_seconds


def measure_drift_into(state: ClockDriftState, *, probe: ClockDriftProbe) -> None:
    """Run ``probe`` once and update ``state`` — never raises: a probe
    failure (no network, a bad server, ...) keeps the last known value
    rather than reporting a misleading zero drift."""
    try:
        state.drift_seconds = probe()
    except Exception:  # noqa: BLE001 - a probe failure must never crash the caller's loop
        return


def ntp_drift_probe(
    clock: Clock, *, server: str = "pool.ntp.org", port: int = 123, timeout: float = 2.0
) -> float:
    """A minimal SNTP (RFC 2030) client query. Production-only."""
    packet = b"\x1b" + 47 * b"\0"
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        sock.settimeout(timeout)
        sock.sendto(packet, (server, port))
        response, _ = sock.recvfrom(48)
    fields = struct.unpack("!12I", response)
    reference = datetime.fromtimestamp(fields[10] - _NTP_EPOCH_OFFSET_SECONDS, tz=UTC)
    return (reference - clock.now_utc()).total_seconds()
