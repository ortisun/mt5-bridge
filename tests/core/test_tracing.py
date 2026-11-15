"""Bridge tracing bootstrap (plan §84): exporter selection, no export by default."""

from mt5_bridge.core.tracing import ExporterKind, TelemetrySettings, configure_tracing


def test_defaults_to_no_export() -> None:
    settings = TelemetrySettings.from_env({})
    assert settings.exporter is ExporterKind.NONE
    assert settings.otlp_endpoint is None


def test_an_unknown_exporter_name_falls_back_to_none() -> None:
    settings = TelemetrySettings.from_env({"MT5_BRIDGE_OTEL_EXPORTER": "carrier-pigeon"})
    assert settings.exporter is ExporterKind.NONE


def test_reads_the_otlp_endpoint() -> None:
    settings = TelemetrySettings.from_env(
        {
            "MT5_BRIDGE_OTEL_EXPORTER": "otlp",
            "MT5_BRIDGE_OTEL_EXPORTER_OTLP_ENDPOINT": "http://collector:4318",
        }
    )
    assert settings.exporter is ExporterKind.OTLP
    assert settings.otlp_endpoint == "http://collector:4318"


def test_configure_tracing_with_no_exporter_still_returns_a_usable_tracer() -> None:
    bootstrap = configure_tracing({})
    assert bootstrap.settings.exporter is ExporterKind.NONE
    with bootstrap.tracer.start_as_current_span("noop"):
        pass
