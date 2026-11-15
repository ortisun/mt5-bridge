"""JSON log formatting for the bridge process (plan §3.13, §84): one OCSF event per line.

Every record renders as an event of the Open Cybersecurity Schema Framework
(``core.ocsf`` owns the vocabulary, ``docs/ocsf.md`` the mapping). A call
site chooses the class with ``extra=ocsf.log_extra(...)``; a record without
one is an Application Error when it is an error or carries an exception, and
a Base Event otherwise, so a plain ``logger.info("...")`` is still a valid
event. ``extra=`` fields that are not OCSF attributes go under ``unmapped``.

``core.log_redaction.SecretRedactingFilter`` always runs first -- it is
attached to the same handlers this formatter renders for -- so every field
read here (message, extras including the OCSF attributes, exception, stack
text) is already scrubbed of the process's known secret values; this module
only serializes what the filter already made safe.
"""

from __future__ import annotations

import json
import logging
import uuid
from collections.abc import Mapping
from typing import cast

from opentelemetry import trace

from mt5_bridge.core import ocsf
from mt5_bridge.core.log_redaction import STANDARD_LOG_RECORD_FIELDS

SERVICE_NAME = ocsf.PRODUCT_NAME
SERVICE_VERSION = ocsf.PRODUCT_VERSION
LOG_ACTIVITY_NAME = "Log"
_UNSERIALIZABLE = "[log record could not be serialized as JSON]"
_JSON_SEPARATORS = (",", ":")
_MERGED_OBJECTS = ("metadata", "unmapped")


def current_trace_context() -> tuple[str | None, str | None]:
    """Hex ``(trace_id, span_id)`` of the active span, or ``(None, None)``."""
    context = trace.get_current_span().get_span_context()
    if not context.is_valid:
        return None, None
    return format(context.trace_id, "032x"), format(context.span_id, "016x")


def _dumps(event: Mapping[str, object]) -> str:
    return json.dumps(event, default=str, ensure_ascii=False, separators=_JSON_SEPARATORS)


def _mapping(value: object) -> dict[str, object]:
    return dict(cast("Mapping[str, object]", value)) if isinstance(value, Mapping) else {}


def _default_classification(record: logging.LogRecord) -> dict[str, object]:
    if record.levelno >= logging.ERROR or record.exc_text:
        return ocsf.classification(ocsf.APPLICATION_ERROR, ocsf.ErrorActivity.GENERAL_ERROR)
    return ocsf.classification(
        ocsf.BASE_EVENT, ocsf.BaseActivity.OTHER, activity_name=LOG_ACTIVITY_NAME
    )


class JsonLogFormatter(logging.Formatter):
    """One OCSF event per line: classification, time, severity, message,
    ``metadata`` (schema version, product, logger name, level, a unique id),
    the class's own attributes, then every other already-scrubbed field under
    ``unmapped``. Never raises: a record that cannot be serialized degrades to
    a minimal, still-valid Application Error event."""

    def __init__(self, *, deployment_environment: str | None = None) -> None:
        super().__init__()
        self._labels = [deployment_environment] if deployment_environment else None

    def format(self, record: logging.LogRecord) -> str:
        try:
            return _dumps(self._event(record))
        except Exception:  # noqa: BLE001 - formatting must never crash the handler
            return _dumps(self._fallback(record))

    def _metadata(self, record: logging.LogRecord) -> dict[str, object]:
        return ocsf.metadata(
            log_name=record.name,
            log_level=record.levelname,
            uid=uuid.uuid4().hex,
            labels=self._labels,
        )

    def _event(self, record: logging.LogRecord) -> dict[str, object]:
        unmapped: dict[str, object] = {
            name: value
            for name, value in record.__dict__.items()
            if name not in STANDARD_LOG_RECORD_FIELDS and name != ocsf.LOG_EXTRA_KEY
        }
        fragment = _mapping(record.__dict__.get(ocsf.LOG_EXTRA_KEY))
        class_uid = fragment.get("class_uid")
        event_class = ocsf.EVENT_CLASSES.get(class_uid) if isinstance(class_uid, int) else None
        if event_class is None:
            if fragment:  # not an event of a class the bridge knows: keep it, unmapped
                unmapped[ocsf.LOG_EXTRA_KEY] = fragment
            fragment = _default_classification(record)
            event_class = ocsf.EVENT_CLASSES[cast("int", fragment["class_uid"])]

        event = {name: value for name, value in fragment.items() if name not in _MERGED_OBJECTS}
        event.setdefault("time", int(record.created * 1000))
        if "severity_id" not in event:
            event.update(ocsf.severity(ocsf.severity_for_log_level(record.levelno)))
        event.setdefault("message", record.getMessage())

        metadata = self._metadata(record)
        metadata.update(_mapping(fragment.get("metadata")))
        trace_id, span_id = current_trace_context()
        if trace_id is not None and span_id is not None:
            attributes, unmapped_ids = ocsf.trace_attributes(event_class, trace_id, span_id)
            if attributes:
                event.update(attributes)
                metadata["profiles"] = [ocsf.TRACE_PROFILE]
            unmapped.update(unmapped_ids)
        event["metadata"] = metadata

        if record.exc_text:
            unmapped["exception"] = record.exc_text
        if record.stack_info:
            unmapped["stack"] = record.stack_info
        unmapped.update(_mapping(fragment.get("unmapped")))
        if unmapped:
            event["unmapped"] = unmapped
        return event

    def _fallback(self, record: logging.LogRecord) -> dict[str, object]:
        return ocsf.event(
            ocsf.APPLICATION_ERROR,
            ocsf.ErrorActivity.GENERAL_ERROR,
            time_ms=int(record.created * 1000),
            level=ocsf.severity_for_log_level(record.levelno),
            message=_UNSERIALIZABLE,
            metadata_attributes={"log_name": str(record.name), "log_level": str(record.levelname)},
        )


__all__ = [
    "LOG_ACTIVITY_NAME",
    "SERVICE_NAME",
    "SERVICE_VERSION",
    "JsonLogFormatter",
    "current_trace_context",
]
