"""JSON log formatting for the bridge process (plan §3.13, §84).

``core.log_redaction.SecretRedactingFilter`` always runs first -- it is
attached to the same handlers this formatter renders for -- so every field
read here (message, extras, exception, stack text) is already scrubbed of
the process's known secret values; this module only serializes what the
filter already made safe.
"""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime

from opentelemetry import trace

from mt5_bridge.core.log_redaction import STANDARD_LOG_RECORD_FIELDS

SERVICE_NAME = "mt5-bridge"
SERVICE_VERSION = "1.0.0"

_JSON_SEPARATORS = (",", ":")


def current_trace_context() -> tuple[str | None, str | None]:
    """Hex ``(trace_id, span_id)`` of the active span, or ``(None, None)``."""
    context = trace.get_current_span().get_span_context()
    if not context.is_valid:
        return None, None
    return format(context.trace_id, "032x"), format(context.span_id, "016x")


class JsonLogFormatter(logging.Formatter):
    """One JSON object per line: resource identity, trace/span ids, message,
    then every already-scrubbed application field. Never raises: a record
    that cannot be serialized degrades to a minimal, still-valid JSON line."""

    def __init__(self, *, deployment_environment: str | None = None) -> None:
        super().__init__()
        self._deployment_environment = deployment_environment

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, object] = {
            "timestamp": datetime.fromtimestamp(record.created, tz=UTC).isoformat(
                timespec="milliseconds"
            ),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
            "service.name": SERVICE_NAME,
            "service.version": SERVICE_VERSION,
        }
        if self._deployment_environment:
            payload["deployment.environment.name"] = self._deployment_environment
        trace_id, span_id = current_trace_context()
        if trace_id is not None:
            payload["trace_id"] = trace_id
            payload["span_id"] = span_id
        for name, value in record.__dict__.items():
            if name not in STANDARD_LOG_RECORD_FIELDS:
                payload[name] = value
        if record.exc_text:
            payload["exception"] = record.exc_text
        if record.stack_info:
            payload["stack"] = record.stack_info
        try:
            return json.dumps(payload, default=str, ensure_ascii=False, separators=_JSON_SEPARATORS)
        except Exception:  # noqa: BLE001 - formatting must never crash the handler
            return json.dumps(
                {
                    "timestamp": payload["timestamp"],
                    "level": payload["level"],
                    "logger": payload["logger"],
                    "message": "[log record could not be serialized as JSON]",
                },
                separators=_JSON_SEPARATORS,
            )


__all__ = ["SERVICE_NAME", "SERVICE_VERSION", "JsonLogFormatter", "current_trace_context"]
