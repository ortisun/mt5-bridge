"""OpenTelemetry tracing bootstrap for the bridge process (plan §84).

One process-local
``TracerProvider``, exporter selected by non-secret ``MT5_BRIDGE_OTEL_*``
configuration, defaulting to no network export. No manual request spans are
added in this delivery (no auto-instrumentation dependency added either);
the provider exists so a future instrumentation library or manual span has
something real to attach to, and ``core.metrics``'s Prometheus reader does
not depend on this module at all.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum

from opentelemetry import trace
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor, ConsoleSpanExporter, SpanExporter

_LOG = logging.getLogger("mt5_bridge.tracing")

SERVICE_NAME = "mt5-bridge"


class ExporterKind(StrEnum):
    NONE = "none"
    CONSOLE = "console"
    OTLP = "otlp"


@dataclass(frozen=True, slots=True)
class TelemetrySettings:
    exporter: ExporterKind
    otlp_endpoint: str | None

    @classmethod
    def from_env(cls, env: Mapping[str, str]) -> TelemetrySettings:
        raw = env.get("MT5_BRIDGE_OTEL_EXPORTER", ExporterKind.NONE.value).strip().lower()
        try:
            exporter = ExporterKind(raw)
        except ValueError:
            _LOG.warning("Unknown MT5_BRIDGE_OTEL_EXPORTER=%r; defaulting to no export", raw)
            exporter = ExporterKind.NONE
        return cls(
            exporter=exporter,
            otlp_endpoint=env.get("MT5_BRIDGE_OTEL_EXPORTER_OTLP_ENDPOINT") or None,
        )


def _build_exporter(settings: TelemetrySettings) -> SpanExporter | None:
    if settings.exporter is ExporterKind.NONE:
        return None
    if settings.exporter is ExporterKind.CONSOLE:
        return ConsoleSpanExporter()
    if settings.otlp_endpoint is None:
        _LOG.warning(
            "MT5_BRIDGE_OTEL_EXPORTER=otlp requires MT5_BRIDGE_OTEL_EXPORTER_OTLP_ENDPOINT; "
            "tracing stays disabled"
        )
        return None
    from opentelemetry.exporter.otlp.proto.http.trace_exporter import (  # noqa: PLC0415
        OTLPSpanExporter,
    )

    return OTLPSpanExporter(endpoint=settings.otlp_endpoint)


@dataclass(frozen=True, slots=True)
class TracingBootstrap:
    settings: TelemetrySettings
    provider: TracerProvider
    tracer: trace.Tracer


def configure_tracing(env: Mapping[str, str]) -> TracingBootstrap:
    settings = TelemetrySettings.from_env(env)
    provider = TracerProvider(resource=Resource.create({"service.name": SERVICE_NAME}))
    exporter = _build_exporter(settings)
    if exporter is not None:
        provider.add_span_processor(BatchSpanProcessor(exporter))
    try:
        trace.set_tracer_provider(provider)
    except Exception:  # noqa: BLE001 - a second registration must never crash boot
        _LOG.debug("Global TracerProvider was already set; keeping the existing one")
    return TracingBootstrap(
        settings=settings, provider=provider, tracer=provider.get_tracer(SERVICE_NAME)
    )


__all__ = [
    "ExporterKind",
    "SERVICE_NAME",
    "TelemetrySettings",
    "TracingBootstrap",
    "configure_tracing",
]
