"""JsonLogFormatter: one OCSF event per record, redaction runs first (plan §3.13, §84)."""

import io
import json
import logging
from typing import Any

from opentelemetry import trace

from mt5_bridge.core import ocsf
from mt5_bridge.core.log_formatting import JsonLogFormatter
from mt5_bridge.core.log_redaction import SecretRedactingFilter

from testkit.contracts import assert_is_ocsf_event

TRACE_ID = 0x0AF7651916CD43DD8448EB211C80319C
SPAN_ID = 0x00F067AA0BA902B7


class _Capture:
    """A logger that renders through the formatter (and the redacting filter,
    as in production) into memory; ``line`` is the last event written."""

    def __init__(
        self,
        name: str,
        *,
        deployment_environment: str | None = None,
        secrets: frozenset[str] | None = None,
    ) -> None:
        self._stream = io.StringIO()
        self.logger = logging.getLogger(name)
        self.logger.setLevel(logging.DEBUG)
        self.logger.propagate = False
        self._handler = logging.StreamHandler(self._stream)
        self._handler.setFormatter(JsonLogFormatter(deployment_environment=deployment_environment))
        if secrets is not None:
            self._handler.addFilter(SecretRedactingFilter(secrets))
        self.logger.addHandler(self._handler)

    def close(self) -> None:
        self.logger.removeHandler(self._handler)

    @property
    def output(self) -> str:
        return self._stream.getvalue()

    @property
    def line(self) -> dict[str, Any]:
        event: dict[str, Any] = json.loads(self.output.splitlines()[-1])
        return event


def test_a_plain_record_is_a_valid_base_event() -> None:
    capture = _Capture("test.log_formatting.plain", deployment_environment="paper")
    try:
        capture.logger.info("hello %s", "world")
    finally:
        capture.close()

    line = capture.line
    assert_is_ocsf_event(line)
    assert line["class_uid"] == 0
    assert line["class_name"] == "Base Event"
    assert line["category_uid"] == 0
    assert line["activity_id"] == 99
    assert line["activity_name"] == "Log"
    assert line["type_uid"] == 99
    assert line["severity_id"] == 1
    assert line["severity"] == "Informational"
    assert line["message"] == "hello world"
    assert isinstance(line["time"], int)
    metadata = line["metadata"]
    assert metadata["version"] == "1.9.0"
    assert metadata["product"] == {
        "name": "mt5-bridge",
        "vendor_name": ocsf.PRODUCT_VENDOR_NAME,
        "version": ocsf.PRODUCT_VERSION,
    }
    assert metadata["log_name"] == "test.log_formatting.plain"
    assert metadata["log_level"] == "INFO"
    assert metadata["labels"] == ["paper"]
    assert len(metadata["uid"]) == 32
    assert "trace" not in line
    assert "unmapped" not in line


def test_every_record_has_its_own_uid() -> None:
    capture = _Capture("test.log_formatting.uid")
    try:
        capture.logger.info("one")
        capture.logger.info("two")
    finally:
        capture.close()

    first, second = (json.loads(text) for text in capture.output.splitlines())
    assert first["metadata"]["uid"] != second["metadata"]["uid"]


def test_the_redacting_filter_runs_before_this_formatter() -> None:
    capture = _Capture("test.log_formatting.redacted", secrets=frozenset({"pw-secret-value"}))
    try:
        capture.logger.info("connecting with password %s", "pw-secret-value")
    finally:
        capture.close()

    assert "pw-secret-value" not in capture.output
    assert capture.line["message"] == "connecting with password [redacted]"


def test_a_secret_inside_the_ocsf_attributes_is_redacted() -> None:
    capture = _Capture("test.log_formatting.redacted_ocsf", secrets=frozenset({"pw-secret-value"}))
    try:
        capture.logger.info(
            "called",
            extra=ocsf.log_extra(
                ocsf.API_ACTIVITY,
                ocsf.ApiActivity.CREATE,
                status_detail="rejected pw-secret-value",
                attributes=ocsf.api_call_attributes(
                    account_id="12345678",
                    method="POST",
                    path="/v1/orders?password=pw-secret-value",
                    src_host="10.0.0.7",
                    src_port=5000,
                ),
                unmapped={"note": ["pw-secret-value"]},
            ),
        )
    finally:
        capture.close()

    assert "pw-secret-value" not in capture.output
    line = capture.line
    assert_is_ocsf_event(line)
    assert line["http_request"]["url"]["path"] == "/v1/orders?password=[redacted]"
    assert line["unmapped"]["note"] == ["[redacted]"]


def test_extra_fields_with_no_ocsf_home_go_under_unmapped() -> None:
    capture = _Capture("test.log_formatting.extras")
    try:
        capture.logger.info("order accepted", extra={"client_order_id": "abc-1", "attempt": 2})
    finally:
        capture.close()

    line = capture.line
    assert_is_ocsf_event(line)
    assert line["unmapped"] == {"client_order_id": "abc-1", "attempt": 2}
    assert "client_order_id" not in line


def test_a_call_site_chooses_the_class() -> None:
    capture = _Capture("test.log_formatting.class")
    try:
        capture.logger.info(
            "mt5-bridge starting", extra=ocsf.lifecycle_log_extra(ocsf.LifecycleActivity.START)
        )
    finally:
        capture.close()

    line = capture.line
    assert_is_ocsf_event(line)
    assert line["class_uid"] == 6002
    assert line["class_name"] == "Application Lifecycle"
    assert line["activity_id"] == 3
    assert line["activity_name"] == "Start"
    assert line["type_uid"] == 600203
    assert line["status_id"] == 1
    assert line["app"]["name"] == "mt5-bridge"
    assert line["message"] == "mt5-bridge starting"
    assert "unmapped" not in line


def test_a_record_with_an_exception_is_an_application_error_with_the_traceback() -> None:
    """Only the redacting filter renders ``exc_info`` into ``exc_text`` (it
    always runs first in production, per this module's own docstring); a
    bare formatter with no filter attached never sees exception text."""
    capture = _Capture("test.log_formatting.exc", secrets=frozenset())
    try:
        try:
            raise ValueError("boom")
        except ValueError:
            capture.logger.exception("failed")
    finally:
        capture.close()

    line = capture.line
    assert_is_ocsf_event(line)
    assert line["class_uid"] == 6008
    assert line["activity_name"] == "General Error"
    assert line["severity_id"] == 4
    assert "ValueError" in line["unmapped"]["exception"]
    assert "boom" in line["unmapped"]["exception"]


def test_a_warning_is_a_base_event_of_medium_severity() -> None:
    capture = _Capture("test.log_formatting.warning")
    try:
        capture.logger.warning("careful")
    finally:
        capture.close()

    line = capture.line
    assert_is_ocsf_event(line)
    assert (line["class_uid"], line["severity_id"], line["severity"]) == (0, 3, "Medium")


def _in_span() -> trace.Span:
    context = trace.SpanContext(TRACE_ID, SPAN_ID, is_remote=False)
    return trace.NonRecordingSpan(context)


def test_inside_a_span_an_api_activity_carries_the_trace_profile() -> None:
    capture = _Capture("test.log_formatting.trace_profile")
    try:
        with trace.use_span(_in_span()):
            capture.logger.info(
                "called",
                extra=ocsf.log_extra(
                    ocsf.API_ACTIVITY,
                    ocsf.ApiActivity.READ,
                    attributes=ocsf.api_call_attributes(
                        account_id="12345678",
                        method="GET",
                        path="/v1/orders",
                        src_host="10.0.0.7",
                        src_port=5000,
                    ),
                ),
            )
    finally:
        capture.close()

    line = capture.line
    assert_is_ocsf_event(line)
    assert line["trace"] == {"uid": format(TRACE_ID, "032x")}
    assert line["metadata"]["profiles"] == ["trace"]
    assert line["unmapped"] == {"span_id": format(SPAN_ID, "016x")}


def test_inside_a_span_a_class_without_the_trace_profile_keeps_the_ids_unmapped() -> None:
    capture = _Capture("test.log_formatting.trace_unmapped")
    try:
        with trace.use_span(_in_span()):
            capture.logger.info("plain")
    finally:
        capture.close()

    line = capture.line
    assert_is_ocsf_event(line)
    assert "trace" not in line
    assert "profiles" not in line["metadata"]
    assert line["unmapped"] == {
        "trace_id": format(TRACE_ID, "032x"),
        "span_id": format(SPAN_ID, "016x"),
    }


def test_an_ocsf_extra_of_an_unknown_class_is_kept_unmapped() -> None:
    capture = _Capture("test.log_formatting.unknown_class")
    try:
        capture.logger.info("odd", extra={ocsf.LOG_EXTRA_KEY: {"class_uid": 424242}})
    finally:
        capture.close()

    line = capture.line
    assert_is_ocsf_event(line)
    assert line["class_uid"] == 0
    assert line["unmapped"] == {"ocsf": {"class_uid": 424242}}


def test_a_record_that_cannot_be_serialized_still_renders_a_valid_event() -> None:
    capture = _Capture("test.log_formatting.unserializable")
    try:
        capture.logger.warning("odd", extra={"bad": {("a", "tuple"): 1}})
    finally:
        capture.close()

    line = capture.line
    assert_is_ocsf_event(line)
    assert line["class_uid"] == 6008
    assert line["message"] == "[log record could not be serialized as JSON]"
    assert line["severity_id"] == 3
    assert line["metadata"]["log_name"] == "test.log_formatting.unserializable"
