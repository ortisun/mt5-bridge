"""The one OpenTelemetry bootstrap of the bridge process (plan §84).

``configure_observability`` reads non-secret environment variables and builds
the three signals -- traces, metrics, logs -- over one shared ``Resource``:

- a ``TracerProvider`` whose spans come from ``core.tracing``;
- a ``MeterProvider`` that always feeds the Prometheus registry behind
  ``GET /v1/metrics`` (``core.metrics``) and, when configured, also pushes
  over OTLP;
- a ``LoggerProvider`` fed by ``core.log_export.OtelLogHandler``, a second
  ``logging`` handler beside the stdout JSON one.

The default is no network export: every exporter is ``none`` until a variable
selects one, and ``otlp`` without an endpoint stays disabled with a warning.
The standard ``OTEL_*`` variables are honoured; the older ``MT5_BRIDGE_OTEL_*``
ones keep working and win where both are set (docs/observability.md has the
table).

Building the providers registers nothing globally, so tests build as many as
they like. ``Observability.install`` is the process-wide step ``main()``
takes once: it registers the providers as the OpenTelemetry globals, attaches
the log handler to the root logger and registers ``shutdown`` to run at exit,
which flushes and closes all three signals.
"""

from __future__ import annotations

import atexit
import logging
import os
import platform
import socket
import sys
import uuid
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from importlib import metadata
from urllib.parse import unquote

from opentelemetry import _logs as logs_api
from opentelemetry import metrics as metrics_api
from opentelemetry import trace as trace_api
from opentelemetry.metrics import Meter
from opentelemetry.sdk._logs import LoggerProvider, LogRecordProcessor
from opentelemetry.sdk._logs.export import (
    BatchLogRecordProcessor,
    ConsoleLogRecordExporter,
    LogRecordExporter,
)
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import (
    ConsoleMetricExporter,
    MetricExporter,
    MetricReader,
    PeriodicExportingMetricReader,
)
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import SpanProcessor, TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor, ConsoleSpanExporter, SpanExporter
from opentelemetry.sdk.trace.sampling import (
    ALWAYS_OFF,
    ALWAYS_ON,
    ParentBased,
    Sampler,
    TraceIdRatioBased,
)
from prometheus_client import CollectorRegistry

from mt5_bridge.core.log_export import OtelLogHandler
from mt5_bridge.core.log_redaction import SecretRedactingFilter
from mt5_bridge.core.metrics import configure_metrics
from mt5_bridge.core.metrics_port import Metrics
from mt5_bridge.core.tracing import register_span_secrets

_LOG = logging.getLogger("mt5_bridge.observability")

DEFAULT_SERVICE_NAME = "mt5-bridge"
DISTRIBUTION_NAME = "mt5-bridge"
DEFAULT_SAMPLER = "parentbased_always_on"
DEFAULT_METRIC_EXPORT_INTERVAL_SECONDS = 60.0

_TRUE_VALUES = frozenset({"1", "true", "yes", "on"})
_SIGNAL_PATHS = ("/v1/traces", "/v1/metrics", "/v1/logs")
_HOST_ARCH = {"x86_64": "amd64", "amd64": "amd64", "arm64": "arm64", "aarch64": "arm64"}


class ExporterKind(StrEnum):
    NONE = "none"
    CONSOLE = "console"
    OTLP = "otlp"


@dataclass(frozen=True, slots=True)
class SignalSettings:
    exporter: ExporterKind
    otlp_endpoint: str | None
    """The full URL of this signal's OTLP/HTTP endpoint, signal path included."""


@dataclass(frozen=True, slots=True)
class ObservabilitySettings:
    service_name: str
    deployment_environment: str | None
    resource_attributes: Mapping[str, str]
    traces: SignalSettings
    metrics: SignalSettings
    logs: SignalSettings
    sampler: str
    sampler_ratio: float
    metric_export_interval_seconds: float

    @classmethod
    def from_env(cls, env: Mapping[str, str]) -> ObservabilitySettings:
        resource_attributes = _parse_resource_attributes(env.get("OTEL_RESOURCE_ATTRIBUTES", ""))
        disabled = env.get("OTEL_SDK_DISABLED", "").strip().lower() in _TRUE_VALUES
        if (env.get("OTEL_EXPORTER_OTLP_PROTOCOL") or "http/protobuf") != "http/protobuf":
            _LOG.warning("Only OTLP over http/protobuf is supported; using it")
        return cls(
            service_name=(
                env.get("OTEL_SERVICE_NAME")
                or resource_attributes.get("service.name")
                or DEFAULT_SERVICE_NAME
            ),
            deployment_environment=env.get("MT5_BRIDGE_ENVIRONMENT") or None,
            resource_attributes=resource_attributes,
            # MT5_BRIDGE_OTEL_EXPORTER predates the standard variable and has
            # always selected the trace exporter only.
            traces=_signal(env, "traces", ("MT5_BRIDGE_OTEL_EXPORTER", "OTEL_TRACES_EXPORTER"))
            if not disabled
            else _DISABLED,
            metrics=_signal(env, "metrics", ("OTEL_METRICS_EXPORTER",))
            if not disabled
            else _DISABLED,
            logs=_signal(env, "logs", ("OTEL_LOGS_EXPORTER",)) if not disabled else _DISABLED,
            sampler=(env.get("OTEL_TRACES_SAMPLER") or DEFAULT_SAMPLER).strip().lower(),
            sampler_ratio=_float(env, "OTEL_TRACES_SAMPLER_ARG", 1.0),
            metric_export_interval_seconds=_float(
                env, "OTEL_METRIC_EXPORT_INTERVAL", DEFAULT_METRIC_EXPORT_INTERVAL_SECONDS * 1000
            )
            / 1000,
        )


_DISABLED = SignalSettings(exporter=ExporterKind.NONE, otlp_endpoint=None)


def _float(env: Mapping[str, str], name: str, default: float) -> float:
    raw = env.get(name)
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError:
        _LOG.warning("%s=%r is not a number; using %s", name, raw, default)
        return default


def _parse_resource_attributes(raw: str) -> dict[str, str]:
    """``key1=value1,key2=value2`` with percent-encoded values (OTel resource SDK spec)."""
    attributes: dict[str, str] = {}
    for item in raw.split(","):
        key, separator, value = item.partition("=")
        if separator and key.strip():
            attributes[key.strip()] = unquote(value.strip())
    return attributes


def _signal(
    env: Mapping[str, str], signal: str, exporter_variables: Sequence[str]
) -> SignalSettings:
    exporter = ExporterKind.NONE
    for name in exporter_variables:
        raw = env.get(name, "").strip().lower()
        if not raw:
            continue
        if raw == "prometheus" and signal == "metrics":
            break  # the pull endpoint is always on; nothing more to select
        try:
            exporter = ExporterKind(raw)
        except ValueError:
            _LOG.warning("Unknown %s=%r; %s are not exported", name, raw, signal)
        break
    endpoint = _otlp_endpoint(env, signal)
    if exporter is ExporterKind.OTLP and endpoint is None:
        _LOG.warning(
            "The otlp exporter for %s needs OTEL_EXPORTER_OTLP_ENDPOINT "
            "(or MT5_BRIDGE_OTEL_EXPORTER_OTLP_ENDPOINT); %s are not exported",
            signal,
            signal,
        )
        exporter = ExporterKind.NONE
    return SignalSettings(exporter=exporter, otlp_endpoint=endpoint)


def _otlp_endpoint(env: Mapping[str, str], signal: str) -> str | None:
    """A signal-specific standard variable is used as it is; a base URL (the
    bridge's own variable first, then the standard one) gets ``/v1/<signal>``."""
    specific = env.get(f"OTEL_EXPORTER_OTLP_{signal.upper()}_ENDPOINT")
    if specific:
        return specific
    base = env.get("MT5_BRIDGE_OTEL_EXPORTER_OTLP_ENDPOINT") or env.get(
        "OTEL_EXPORTER_OTLP_ENDPOINT"
    )
    if not base:
        return None
    base = base.rstrip("/")
    for path in _SIGNAL_PATHS:  # tolerate a base that already names one signal
        base = base.removesuffix(path)
    return f"{base}/v1/{signal}"


def service_version() -> str:
    try:
        return metadata.version(DISTRIBUTION_NAME)
    except metadata.PackageNotFoundError:
        return "unknown"


def build_resource(settings: ObservabilitySettings) -> Resource:
    """Identity shared by the three signals (OTel resource semantic conventions).

    Later entries win: detected host/process facts, then the bridge's own
    version and environment, then ``OTEL_RESOURCE_ATTRIBUTES``, then the
    service name. The process command line is left out on purpose.
    """
    machine = platform.machine().lower()
    attributes: dict[str, str | int] = {
        "host.name": socket.gethostname(),
        "host.arch": _HOST_ARCH.get(machine, machine),
        "os.type": platform.system().lower(),
        "process.pid": os.getpid(),
        "process.executable.name": os.path.basename(sys.executable),
        "process.runtime.name": sys.implementation.name,
        "process.runtime.version": platform.python_version(),
        "service.instance.id": str(uuid.uuid4()),
        "service.version": service_version(),
    }
    if settings.deployment_environment:
        attributes["deployment.environment.name"] = settings.deployment_environment
    attributes.update(settings.resource_attributes)
    attributes["service.name"] = settings.service_name
    return Resource.create(attributes)


def build_sampler(settings: ObservabilitySettings) -> Sampler:
    """``OTEL_TRACES_SAMPLER`` / ``OTEL_TRACES_SAMPLER_ARG`` (OTel SDK configuration spec)."""
    ratio = min(max(settings.sampler_ratio, 0.0), 1.0)
    samplers: dict[str, Sampler] = {
        "always_on": ALWAYS_ON,
        "always_off": ALWAYS_OFF,
        "traceidratio": TraceIdRatioBased(ratio),
        "parentbased_always_on": ParentBased(ALWAYS_ON),
        "parentbased_always_off": ParentBased(ALWAYS_OFF),
        "parentbased_traceidratio": ParentBased(TraceIdRatioBased(ratio)),
    }
    sampler = samplers.get(settings.sampler)
    if sampler is None:
        _LOG.warning("Unknown OTEL_TRACES_SAMPLER=%r; using %s", settings.sampler, DEFAULT_SAMPLER)
        return samplers[DEFAULT_SAMPLER]
    return sampler


def build_span_exporter(settings: SignalSettings) -> SpanExporter | None:
    if settings.exporter is ExporterKind.CONSOLE:
        return ConsoleSpanExporter()
    if settings.exporter is ExporterKind.OTLP:
        from opentelemetry.exporter.otlp.proto.http.trace_exporter import (  # noqa: PLC0415
            OTLPSpanExporter,
        )

        return OTLPSpanExporter(endpoint=settings.otlp_endpoint)
    return None


def build_metric_exporter(settings: SignalSettings) -> MetricExporter | None:
    if settings.exporter is ExporterKind.CONSOLE:
        return ConsoleMetricExporter()
    if settings.exporter is ExporterKind.OTLP:
        from opentelemetry.exporter.otlp.proto.http.metric_exporter import (  # noqa: PLC0415
            OTLPMetricExporter,
        )

        return OTLPMetricExporter(endpoint=settings.otlp_endpoint)
    return None


def build_log_exporter(settings: SignalSettings) -> LogRecordExporter | None:
    if settings.exporter is ExporterKind.CONSOLE:
        return ConsoleLogRecordExporter()
    if settings.exporter is ExporterKind.OTLP:
        from opentelemetry.exporter.otlp.proto.http._log_exporter import (  # noqa: PLC0415
            OTLPLogExporter,
        )

        return OTLPLogExporter(endpoint=settings.otlp_endpoint)
    return None


@dataclass(slots=True)
class Observability:
    """The configured signals and the single install/shutdown path."""

    settings: ObservabilitySettings
    resource: Resource
    tracer_provider: TracerProvider
    meter_provider: MeterProvider
    logger_provider: LoggerProvider
    registry: CollectorRegistry
    meter: Meter
    metrics: Metrics
    span_exporter: SpanExporter | None
    metric_exporter: MetricExporter | None
    log_exporter: LogRecordExporter | None
    log_handler: OtelLogHandler | None
    """``None`` unless a log exporter or processor is configured: with logs
    disabled no second handler exists at all."""
    _redactor: SecretRedactingFilter
    _attached_to: list[logging.Logger] = field(default_factory=list[logging.Logger])
    _shut_down: bool = False

    def redact(self, secrets: Iterable[str]) -> None:
        """Make the log bridge and span error recording scrub these literal values."""
        values = frozenset(secrets)
        self._redactor.add_secrets(values)
        register_span_secrets(values)

    def attach_log_handler(self, logger: logging.Logger) -> None:
        if self.log_handler is not None and self.log_handler not in logger.handlers:
            logger.addHandler(self.log_handler)
            self._attached_to.append(logger)

    def install(self) -> None:
        """Process-wide, once (``main()``): global providers, the root log
        handler, and ``shutdown`` at interpreter exit."""
        trace_api.set_tracer_provider(self.tracer_provider)
        metrics_api.set_meter_provider(self.meter_provider)
        logs_api.set_logger_provider(self.logger_provider)
        self.attach_log_handler(logging.getLogger())
        atexit.register(self.shutdown)

    def shutdown(self, *, timeout_seconds: float = 5.0) -> None:
        """Flush and close the three signals; idempotent and never raises."""
        if self._shut_down:
            return
        self._shut_down = True
        for logger in self._attached_to:
            if self.log_handler is not None:
                logger.removeHandler(self.log_handler)
        self._attached_to.clear()
        timeout_millis = int(timeout_seconds * 1000)
        steps = (
            ("traces", lambda: self.tracer_provider.force_flush(timeout_millis)),
            ("traces", self.tracer_provider.shutdown),
            ("metrics", lambda: self.meter_provider.force_flush(timeout_millis)),
            ("metrics", lambda: self.meter_provider.shutdown(timeout_millis)),
            ("logs", lambda: self.logger_provider.force_flush(timeout_millis)),
            ("logs", self.logger_provider.shutdown),
        )
        for signal, step in steps:
            try:
                step()
            except Exception:  # noqa: BLE001 - exit must not fail on a telemetry backend
                _LOG.warning("Shutting down %s export failed", signal, exc_info=True)


def configure_observability(
    env: Mapping[str, str],
    *,
    span_processors: Sequence[SpanProcessor] = (),
    metric_readers: Sequence[MetricReader] = (),
    log_processors: Sequence[LogRecordProcessor] = (),
) -> Observability:
    """Build the three signals from ``env``; registers nothing process-wide.

    The ``*_processors``/``metric_readers`` arguments add to whatever ``env``
    selects; tests pass in-memory ones.
    """
    settings = ObservabilitySettings.from_env(env)
    resource = build_resource(settings)

    tracer_provider = TracerProvider(sampler=build_sampler(settings), resource=resource)
    span_exporter = build_span_exporter(settings.traces)
    if span_exporter is not None:
        tracer_provider.add_span_processor(BatchSpanProcessor(span_exporter))
    for span_processor in span_processors:
        tracer_provider.add_span_processor(span_processor)

    metric_exporter = build_metric_exporter(settings.metrics)
    readers = list(metric_readers)
    if metric_exporter is not None:
        readers.append(
            PeriodicExportingMetricReader(
                metric_exporter,
                export_interval_millis=settings.metric_export_interval_seconds * 1000,
            )
        )
    metrics_bootstrap = configure_metrics(env, resource=resource, extra_readers=readers)

    logger_provider = LoggerProvider(resource=resource, meter_provider=metrics_bootstrap.provider)
    log_exporter = build_log_exporter(settings.logs)
    if log_exporter is not None:
        logger_provider.add_log_record_processor(BatchLogRecordProcessor(log_exporter))
    for log_processor in log_processors:
        logger_provider.add_log_record_processor(log_processor)
    redactor = SecretRedactingFilter()
    log_handler = (
        OtelLogHandler(logger_provider, redactor=redactor)
        if log_exporter is not None or log_processors
        else None
    )

    return Observability(
        settings=settings,
        resource=resource,
        tracer_provider=tracer_provider,
        meter_provider=metrics_bootstrap.provider,
        logger_provider=logger_provider,
        registry=metrics_bootstrap.registry,
        meter=metrics_bootstrap.meter,
        metrics=metrics_bootstrap.metrics,
        span_exporter=span_exporter,
        metric_exporter=metric_exporter,
        log_exporter=log_exporter,
        log_handler=log_handler,
        _redactor=redactor,
    )


__all__ = [
    "DEFAULT_SERVICE_NAME",
    "ExporterKind",
    "Observability",
    "ObservabilitySettings",
    "SignalSettings",
    "build_log_exporter",
    "build_metric_exporter",
    "build_resource",
    "build_sampler",
    "build_span_exporter",
    "configure_observability",
    "service_version",
]
