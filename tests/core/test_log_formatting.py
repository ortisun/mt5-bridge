"""JsonLogFormatter: one JSON object per record, redaction runs first (plan §3.13, §84)."""

import io
import json
import logging

from mt5_bridge.core.log_formatting import JsonLogFormatter
from mt5_bridge.core.log_redaction import SecretRedactingFilter


def _handler(stream: io.StringIO, *, deployment_environment: str | None = None) -> logging.Handler:
    handler = logging.StreamHandler(stream)
    handler.setFormatter(JsonLogFormatter(deployment_environment=deployment_environment))
    return handler


def test_a_plain_record_renders_as_one_json_object_with_the_mandatory_fields() -> None:
    stream = io.StringIO()
    logger = logging.getLogger("test.log_formatting.plain")
    logger.setLevel(logging.INFO)
    logger.propagate = False
    handler = _handler(stream, deployment_environment="paper")
    logger.addHandler(handler)
    try:
        logger.info("hello %s", "world")
    finally:
        logger.removeHandler(handler)

    line = json.loads(stream.getvalue().splitlines()[-1])
    assert line["level"] == "INFO"
    assert line["logger"] == "test.log_formatting.plain"
    assert line["message"] == "hello world"
    assert line["service.name"] == "mt5-bridge"
    assert line["deployment.environment.name"] == "paper"
    assert "timestamp" in line
    assert "trace_id" not in line


def test_the_redacting_filter_runs_before_this_formatter() -> None:
    stream = io.StringIO()
    logger = logging.getLogger("test.log_formatting.redacted")
    logger.setLevel(logging.INFO)
    logger.propagate = False
    handler = _handler(stream)
    redactor = SecretRedactingFilter({"pw-secret-value"})
    handler.addFilter(redactor)
    logger.addHandler(handler)
    try:
        logger.info("connecting with password %s", "pw-secret-value")
    finally:
        logger.removeHandler(handler)

    output = stream.getvalue()
    assert "pw-secret-value" not in output
    line = json.loads(output.splitlines()[-1])
    assert line["message"] == "connecting with password [redacted]"


def test_extra_fields_are_surfaced_as_top_level_json_keys() -> None:
    stream = io.StringIO()
    logger = logging.getLogger("test.log_formatting.extras")
    logger.setLevel(logging.INFO)
    logger.propagate = False
    handler = _handler(stream)
    logger.addHandler(handler)
    try:
        logger.info("order accepted", extra={"client_order_id": "abc-1", "attempt": 2})
    finally:
        logger.removeHandler(handler)

    line = json.loads(stream.getvalue().splitlines()[-1])
    assert line["client_order_id"] == "abc-1"
    assert line["attempt"] == 2


def test_a_record_with_an_exception_includes_the_traceback_text() -> None:
    """Only the redacting filter renders ``exc_info`` into ``exc_text`` (it
    always runs first in production, per this module's own docstring); a
    bare formatter with no filter attached never sees exception text."""
    stream = io.StringIO()
    logger = logging.getLogger("test.log_formatting.exc")
    logger.setLevel(logging.INFO)
    logger.propagate = False
    handler = _handler(stream)
    handler.addFilter(SecretRedactingFilter())
    logger.addHandler(handler)
    try:
        try:
            raise ValueError("boom")
        except ValueError:
            logger.exception("failed")
    finally:
        logger.removeHandler(handler)

    line = json.loads(stream.getvalue().splitlines()[-1])
    assert "ValueError" in line["exception"]
    assert "boom" in line["exception"]
