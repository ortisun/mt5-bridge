"""Metrics Port: bridge code records a measurement without importing
OpenTelemetry directly at the call site (plan §84). Split from
``core.metrics`` so low-level state classes (``core.kill_switch``,
``core.order_service``) can depend on the Port without ``core.metrics``'s
own import of them creating a cycle: ``core.metrics.register_state_gauges``
imports ``KillSwitch`` for its observable-gauge callback, so
``KillSwitch`` cannot import back from ``core.metrics``.

The Port is deliberately small: a
duration and a count cover every plan §84 minimum metric. Callers pass this
Port as a required dependency; a composition that wants no telemetry
injects ``NullMetrics()`` explicitly, so the choice is visible at the call
site instead of an absent default.
"""

from __future__ import annotations

from typing import Protocol

from opentelemetry.metrics import Counter, Histogram, Meter

AttributeValue = str | bool | int | float | None


class Metrics(Protocol):
    def record_duration(self, name: str, seconds: float, /, **attributes: AttributeValue) -> None:
        """Record one observation of a duration-shaped measurement (a histogram)."""
        ...

    def increment(self, name: str, value: int = 1, /, **attributes: AttributeValue) -> None:
        """Add ``value`` to a monotonic count (a counter)."""
        ...


class NullMetrics:
    """The explicit no-telemetry choice: never exports, never raises, never records."""

    __slots__ = ()

    def record_duration(self, name: str, seconds: float, /, **attributes: AttributeValue) -> None:
        del name, seconds, attributes

    def increment(self, name: str, value: int = 1, /, **attributes: AttributeValue) -> None:
        del name, value, attributes


class OtelMetrics:
    """Adapts one ``Meter`` to the ``Metrics`` Port; instruments are created
    once per name and cached (OTel requires exactly one
    ``create_counter``/``create_histogram`` call per name, never a fresh
    one per recorded point)."""

    def __init__(self, meter: Meter) -> None:
        self._meter = meter
        self._counters: dict[str, Counter] = {}
        self._histograms: dict[str, Histogram] = {}

    @staticmethod
    def _attributes(attributes: dict[str, AttributeValue]) -> dict[str, str | bool | int | float]:
        return {key: value for key, value in attributes.items() if value is not None}

    def record_duration(self, name: str, seconds: float, /, **attributes: AttributeValue) -> None:
        histogram = self._histograms.get(name)
        if histogram is None:
            histogram = self._meter.create_histogram(name, unit="s")
            self._histograms[name] = histogram
        histogram.record(seconds, attributes=self._attributes(attributes))

    def increment(self, name: str, value: int = 1, /, **attributes: AttributeValue) -> None:
        counter = self._counters.get(name)
        if counter is None:
            counter = self._meter.create_counter(name)
            self._counters[name] = counter
        counter.add(value, attributes=self._attributes(attributes))


__all__ = ["AttributeValue", "Metrics", "NullMetrics", "OtelMetrics"]
