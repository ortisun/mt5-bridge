"""The observability bootstrap (plan §84): one resource, three signals, no
network export unless configured, logs that carry the trace id."""

import logging

import pytest
from opentelemetry.exporter.otlp.proto.http._log_exporter import OTLPLogExporter
from opentelemetry.exporter.otlp.proto.http.metric_exporter import OTLPMetricExporter
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.sdk._logs.export import InMemoryLogRecordExporter, SimpleLogRecordProcessor
from opentelemetry.sdk.metrics.export import InMemoryMetricReader
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.sdk.trace.sampling import ALWAYS_OFF, Decision, ParentBased, TraceIdRatioBased

from mt5_bridge.core.observability import (
    ExporterKind,
    ObservabilitySettings,
    SignalSettings,
    build_log_exporter,
    build_metric_exporter,
    build_sampler,
    build_span_exporter,
    configure_observability,
    service_version,
)

from testkit.telemetry import span_id, trace_id

COLLECTOR = "http://collector.invalid:4318"


def test_nothing_is_exported_by_default() -> None:
    observability = configure_observability({})

    for signal in (
        observability.settings.traces,
        observability.settings.metrics,
        observability.settings.logs,
    ):
        assert signal.exporter is ExporterKind.NONE
    assert observability.span_exporter is None
    assert observability.metric_exporter is None
    assert observability.log_exporter is None
    assert observability.log_handler is None


def test_the_three_signals_share_one_resource() -> None:
    observability = configure_observability(
        {
            "MT5_BRIDGE_ENVIRONMENT": "demo",
            "OTEL_RESOURCE_ATTRIBUTES": "service.namespace=trading,team=desk%20a",
        }
    )

    attributes = observability.resource.attributes
    assert attributes["service.name"] == "mt5-bridge"
    assert attributes["service.version"] == service_version() != "unknown"
    assert attributes["deployment.environment.name"] == "demo"
    assert attributes["service.namespace"] == "trading"
    assert attributes["team"] == "desk a"
    for name in ("host.name", "host.arch", "os.type", "process.pid", "process.runtime.version"):
        assert name in attributes
    assert "process.command_line" not in attributes
    assert observability.tracer_provider.resource is observability.resource
    assert observability.logger_provider.resource is observability.resource


def test_otel_service_name_wins_over_the_resource_attribute() -> None:
    from_attribute = ObservabilitySettings.from_env(
        {"OTEL_RESOURCE_ATTRIBUTES": "service.name=from-attributes"}
    )
    from_variable = ObservabilitySettings.from_env(
        {
            "OTEL_RESOURCE_ATTRIBUTES": "service.name=from-attributes",
            "OTEL_SERVICE_NAME": "bridge-demo",
        }
    )

    assert from_attribute.service_name == "from-attributes"
    assert from_variable.service_name == "bridge-demo"


def test_the_standard_variables_select_each_signal_and_its_endpoint() -> None:
    settings = ObservabilitySettings.from_env(
        {
            "OTEL_TRACES_EXPORTER": "otlp",
            "OTEL_METRICS_EXPORTER": "otlp",
            "OTEL_LOGS_EXPORTER": "otlp",
            "OTEL_EXPORTER_OTLP_ENDPOINT": f"{COLLECTOR}/",
            "OTEL_EXPORTER_OTLP_LOGS_ENDPOINT": "http://logs.invalid:4318/custom",
            "OTEL_METRIC_EXPORT_INTERVAL": "15000",
        }
    )

    assert settings.traces == SignalSettings(ExporterKind.OTLP, f"{COLLECTOR}/v1/traces")
    assert settings.metrics == SignalSettings(ExporterKind.OTLP, f"{COLLECTOR}/v1/metrics")
    assert settings.logs == SignalSettings(ExporterKind.OTLP, "http://logs.invalid:4318/custom")
    assert settings.metric_export_interval_seconds == 15.0


def test_the_bridge_variables_keep_working_and_win_over_the_standard_ones() -> None:
    settings = ObservabilitySettings.from_env(
        {
            "MT5_BRIDGE_OTEL_EXPORTER": "otlp",
            "MT5_BRIDGE_OTEL_EXPORTER_OTLP_ENDPOINT": f"{COLLECTOR}/v1/traces",
            "OTEL_TRACES_EXPORTER": "console",
            "OTEL_EXPORTER_OTLP_ENDPOINT": "http://other.invalid:4318",
        }
    )

    assert settings.traces == SignalSettings(ExporterKind.OTLP, f"{COLLECTOR}/v1/traces")
    # The bridge variable has only ever selected the trace exporter.
    assert settings.metrics.exporter is ExporterKind.NONE
    assert settings.logs.exporter is ExporterKind.NONE


def test_otlp_without_an_endpoint_stays_disabled() -> None:
    settings = ObservabilitySettings.from_env({"OTEL_TRACES_EXPORTER": "otlp"})

    assert settings.traces.exporter is ExporterKind.NONE


def test_an_unknown_exporter_name_falls_back_to_none() -> None:
    settings = ObservabilitySettings.from_env({"MT5_BRIDGE_OTEL_EXPORTER": "carrier-pigeon"})

    assert settings.traces.exporter is ExporterKind.NONE


def test_the_prometheus_metrics_exporter_name_adds_no_push_exporter() -> None:
    settings = ObservabilitySettings.from_env({"OTEL_METRICS_EXPORTER": "prometheus"})

    assert settings.metrics.exporter is ExporterKind.NONE


def test_otel_sdk_disabled_switches_every_exporter_off() -> None:
    settings = ObservabilitySettings.from_env(
        {
            "OTEL_SDK_DISABLED": "true",
            "OTEL_TRACES_EXPORTER": "console",
            "OTEL_LOGS_EXPORTER": "console",
        }
    )

    assert settings.traces.exporter is ExporterKind.NONE
    assert settings.logs.exporter is ExporterKind.NONE


def test_otlp_exporters_are_built_for_the_resolved_endpoint_without_connecting() -> None:
    # Constructing an OTLP/HTTP exporter opens no connection; nothing is exported here.
    traces = SignalSettings(ExporterKind.OTLP, f"{COLLECTOR}/v1/traces")
    metrics = SignalSettings(ExporterKind.OTLP, f"{COLLECTOR}/v1/metrics")
    logs = SignalSettings(ExporterKind.OTLP, f"{COLLECTOR}/v1/logs")

    assert isinstance(build_span_exporter(traces), OTLPSpanExporter)
    assert isinstance(build_metric_exporter(metrics), OTLPMetricExporter)
    assert isinstance(build_log_exporter(logs), OTLPLogExporter)


def test_the_sampler_follows_the_standard_variables() -> None:
    default = build_sampler(ObservabilitySettings.from_env({}))
    ratio = build_sampler(
        ObservabilitySettings.from_env(
            {"OTEL_TRACES_SAMPLER": "traceidratio", "OTEL_TRACES_SAMPLER_ARG": "0.25"}
        )
    )
    off = build_sampler(ObservabilitySettings.from_env({"OTEL_TRACES_SAMPLER": "always_off"}))
    unknown = build_sampler(ObservabilitySettings.from_env({"OTEL_TRACES_SAMPLER": "sometimes"}))

    assert isinstance(default, ParentBased)
    assert default.should_sample(None, 1, "span").decision is Decision.RECORD_AND_SAMPLE
    assert isinstance(ratio, TraceIdRatioBased) and ratio.rate == 0.25
    assert off is ALWAYS_OFF
    assert isinstance(unknown, ParentBased)


def test_spans_and_metrics_reach_the_configured_processors() -> None:
    span_exporter = InMemorySpanExporter()
    reader = InMemoryMetricReader()
    observability = configure_observability(
        {},
        span_processors=[SimpleSpanProcessor(span_exporter)],
        metric_readers=[reader],
    )

    with observability.tracer_provider.get_tracer("test").start_as_current_span("work"):
        observability.metrics.increment("mt5_bridge.test.count")

    [finished] = span_exporter.get_finished_spans()
    assert finished.resource is observability.resource
    data = reader.get_metrics_data()
    assert data is not None
    [resource_metrics] = data.resource_metrics
    assert resource_metrics.resource is observability.resource
    assert resource_metrics.scope_metrics[0].metrics[0].name == "mt5_bridge.test.count"


def _log_inside_and_outside_a_span(
    name: str,
) -> tuple[InMemoryLogRecordExporter, InMemorySpanExporter]:
    span_exporter = InMemorySpanExporter()
    log_exporter = InMemoryLogRecordExporter()
    observability = configure_observability(
        {},
        span_processors=[SimpleSpanProcessor(span_exporter)],
        log_processors=[SimpleLogRecordProcessor(log_exporter)],
    )
    observability.redact({"tok-log-secret"})
    logger = logging.getLogger(name)
    logger.setLevel(logging.INFO)
    logger.propagate = False
    observability.attach_log_handler(logger)
    tracer = observability.tracer_provider.get_tracer("test")
    with tracer.start_as_current_span("request"):
        logger.warning("order %s rejected", "co-1", extra={"symbol": "WINZ25"})
        logger.info("password %s", "tok-log-secret")
    logger.info("outside any span")
    observability.shutdown()
    assert logger.handlers == []
    return log_exporter, span_exporter


def test_exported_log_records_carry_the_trace_and_span_ids() -> None:
    log_exporter, span_exporter = _log_inside_and_outside_a_span("test.observability.trace_ids")

    [request_span] = span_exporter.get_finished_spans()
    in_span, _redacted, outside = (item.log_record for item in log_exporter.get_finished_logs())
    assert in_span.body == "order co-1 rejected"
    assert in_span.severity_text == "WARN"
    assert in_span.trace_id == trace_id(request_span)
    assert in_span.span_id == span_id(request_span)
    assert in_span.attributes is not None and in_span.attributes["symbol"] == "WINZ25"
    assert not outside.trace_id


def test_the_redaction_filter_runs_before_a_record_is_exported() -> None:
    log_exporter, _spans = _log_inside_and_outside_a_span("test.observability.redaction")

    bodies = [item.log_record.body for item in log_exporter.get_finished_logs()]
    assert "password [redacted]" in bodies
    assert not any("tok-log-secret" in str(body) for body in bodies)


def test_the_exporters_own_log_records_are_not_fed_back_into_the_export() -> None:
    log_exporter = InMemoryLogRecordExporter()
    observability = configure_observability(
        {}, log_processors=[SimpleLogRecordProcessor(log_exporter)]
    )
    logger = logging.getLogger("opentelemetry.exporter.test")
    logger.propagate = False
    observability.attach_log_handler(logger)
    try:
        logger.error("export failed")
    finally:
        observability.shutdown()

    assert log_exporter.get_finished_logs() == ()


def test_shutdown_is_idempotent() -> None:
    observability = configure_observability({})

    observability.shutdown()
    observability.shutdown()


@pytest.mark.parametrize("raw", ["", "novalue", "=x", "a=1,,b=2"])
def test_malformed_resource_attributes_are_skipped_not_fatal(raw: str) -> None:
    settings = ObservabilitySettings.from_env({"OTEL_RESOURCE_ATTRIBUTES": raw})

    assert set(settings.resource_attributes) <= {"a", "b"}
