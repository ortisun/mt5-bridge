"""Bridge from the standard ``logging`` module to the OpenTelemetry Logs SDK.

``OtelLogHandler`` is a second handler beside the stdout JSON one
(``core.log_formatting``), never a replacement: it turns each record into an
OpenTelemetry log record carrying the active trace and span ids and hands it
to a ``LoggerProvider``, whose processors export it (``core.observability``
decides where). It does not format the JSON line and does not depend on
``core.log_formatting``.

Redaction comes first (plan §84). The handler is built with a
``SecretRedactingFilter`` already attached, so no record reaches the Logs SDK
unscrubbed whatever order the process installs its other handlers in.

Records from the OpenTelemetry SDK itself and from the HTTP client the OTLP
exporter uses are not exported: an exporter failure is logged through those
loggers, and exporting that log line would feed the failure back into the
exporter. They still reach stdout.
"""

from __future__ import annotations

import logging
import time

from opentelemetry import context as otel_context
from opentelemetry._logs import LogRecord, SeverityNumber
from opentelemetry.sdk._logs import LoggerProvider

from mt5_bridge.core.log_redaction import STANDARD_LOG_RECORD_FIELDS, SecretRedactingFilter

_NOT_EXPORTED_LOGGER_PREFIXES = ("opentelemetry", "urllib3", "requests")

_SEVERITY_TEXT = {"WARNING": "WARN", "CRITICAL": "FATAL"}

AttributeValue = str | bool | int | float


def _severity_number(levelno: int) -> SeverityNumber:
    if levelno >= logging.CRITICAL:
        return SeverityNumber.FATAL
    if levelno >= logging.ERROR:
        return SeverityNumber.ERROR
    if levelno >= logging.WARNING:
        return SeverityNumber.WARN
    if levelno >= logging.INFO:
        return SeverityNumber.INFO
    if levelno >= logging.DEBUG:
        return SeverityNumber.DEBUG
    return SeverityNumber.TRACE


def _attribute_value(value: object) -> AttributeValue:
    if isinstance(value, str | bool | int | float):
        return value
    return str(value)


class OtelLogHandler(logging.Handler):
    """Emits each ``logging`` record to ``provider``; never raises."""

    def __init__(
        self,
        provider: LoggerProvider,
        *,
        redactor: SecretRedactingFilter,
        level: int = logging.NOTSET,
    ) -> None:
        super().__init__(level=level)
        self._provider = provider
        self.addFilter(redactor)

    def emit(self, record: logging.LogRecord) -> None:
        if record.name.startswith(_NOT_EXPORTED_LOGGER_PREFIXES):
            return
        try:
            self._provider.get_logger(record.name).emit(self._translate(record))
        except Exception:  # noqa: BLE001 - an export bug must never crash the caller
            self.handleError(record)

    def _translate(self, record: logging.LogRecord) -> LogRecord:
        attributes: dict[str, AttributeValue] = {
            "code.file.path": record.pathname,
            "code.function.name": record.funcName,
            "code.line.number": record.lineno,
        }
        for name, value in record.__dict__.items():
            if name not in STANDARD_LOG_RECORD_FIELDS and value is not None:
                attributes[name] = _attribute_value(value)
        # The redacting filter has already rendered and scrubbed the traceback
        # into exc_text and cleared exc_info.
        if record.exc_text:
            attributes["exception.stacktrace"] = record.exc_text
        if record.stack_info:
            attributes["code.stacktrace"] = record.stack_info
        return LogRecord(
            timestamp=int(record.created * 1e9),
            observed_timestamp=time.time_ns(),
            context=otel_context.get_current(),
            severity_text=_SEVERITY_TEXT.get(record.levelname, record.levelname),
            severity_number=_severity_number(record.levelno),
            body=record.getMessage(),
            attributes=attributes,
        )


__all__ = ["OtelLogHandler"]
